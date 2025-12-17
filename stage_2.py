import re
import sys
import math

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# 1. Input Files
NETLIST_FILE    = "b10_tpi.v"            # Your current netlist
REPORT_FILE     = "stage2_failures.rpt"  # The report from TetraMAX (run report_faults -level 100 > file)

# 2. Output File
OUTPUT_VERILOG  = "b10_compressed.v"

# 3. Strategy Settings
FAULTS_PER_REG  = 4   # Compression Ratio (4 Faults -> 1 Register)

# 4. Standard Cell Library Definitions (SAED32)
# MUX to be inserted
MUX_CELL_NAME   = "MUX21X1_LVT"
# MUX Pin Mapping: 
# A1 = Normal Data (0)
# A2 = Test Data (1)
# S0 = Select
# Y  = Output
MUX_PINS        = {"A": "A1", "B": "A2", "S": "S0", "Y": "Y"} 

# ==============================================================================
# LOGIC IMPLEMENTATION
# ==============================================================================

def get_available_registers(content):
    """ 
    Scans netlist for Flip-Flops to use as observation points.
    Looks for standard instances with .D() connections.
    """
    registers = []
    # Regex to capture: Type, Name, and current D-pin connection
    # Pattern looks for: CellType InstanceName ( ... .D( net ) ... );
    # We allow flexible spacing and newlines.
    pattern = re.compile(r'([\w\\]+)\s+([\w\\]+)\s*\((?:[^;]*?)\.([Dd])\s*\(\s*([\w\\\[\]]+)\s*\)(?:[^;]*?)\);', re.DOTALL)
    
    matches = pattern.findall(content)
    for m in matches:
        reg_type, reg_name, d_pin, d_net = m
        
        # FILTER: Only pick likely Flip-Flops (DFF, FD, or names containing 'reg')
        # This prevents picking buffers or gates by accident.
        if "DFF" in reg_type or "reg" in reg_name or "FD" in reg_type:
            registers.append({
                "type": reg_type, 
                "name": reg_name, 
                "d_pin": d_pin, 
                "d_net": d_net
            })
            
    return registers

def get_fault_targets(filename, content):
    """ 
    Parses TetraMAX report to find failing NETS.
    """
    targets = []
    seen_nets = set()
    
    try:
        with open(filename, 'r') as f:
            for line in f:
                # We specifically want 'NO' (Not Observed) faults
                if "NO" in line:
                    parts = line.split()
                    # TetraMAX format usually ends with: ... code   \path/to/instance/pin
                    full_path = parts[-1] 
                    inst_name = full_path.split('/')[0]
                    
                    # We need the OUTPUT NET of this failing instance.
                    # We search the netlist for the instance and capture its output pin (.Y, .Q, .Z)
                    # Regex looks for: InstanceName ( ... .Y( target_net ) ... )
                    safe_inst = re.escape(inst_name)
                    net_search = re.search(r"{}\s*\([\s\S]*?\.(Y|Z|Q|QN)\s*\(\s*([\w\\\[\]]+)\s*\)".format(safe_inst), content)
                    
                    if net_search:
                        net = net_search.group(2)
                        # Avoid duplicates (observing the same net twice wastes resources)
                        if net not in seen_nets:
                            targets.append(net)
                            seen_nets.add(net)
                            
    except IOError:
        print("Error: Could not read report file: " + filename)
        return []
        
    return targets

def inject_compressed_observation():
    print("--------------------------------------------------")
    print("  DFT AUTOMATION: XOR COMPRESSION & INSERTION")
    print("--------------------------------------------------")
    
    # 1. Read Netlist
    with open(NETLIST_FILE, 'r') as f:
        content = f.read()

    # 2. Analyze Resources
    registers = get_available_registers(content)
    targets   = get_fault_targets(REPORT_FILE, content)

    print("  [*] Netlist Analysis:")
    print("      - Available Registers: {}".format(len(registers)))
    print("      - Unobserved Faults:   {}".format(len(targets)))
    
    if not targets:
        print("  [!] No faults found. Is the report file correct?")
        return

    # 3. Add Global Test Mode Pin
    # We verify if 'test_mode' already exists to avoid errors
    if "input test_mode" not in content:
        print("  [*] Adding global 'test_mode' input pin...")
        # Inject into module arguments: module top ( a, b, ... ) -> module top ( test_mode, a, b ... )
        content = re.sub(r"(module\s+[\w\\]+\s*\()", r"\1 test_mode, ", content, count=1)
        # Inject into definitions: input a; -> input test_mode; input a;
        content = re.sub(r"(input\s)", r"input test_mode;\n  \1", content, count=1)

    # 4. Begin Insertion Loop
    print("  [*] Beginning Insertion (Ratio {}:1)...".format(FAULTS_PER_REG))
    
    fault_idx = 0
    modified_count = 0
    
    for i, host in enumerate(registers):
        # Stop if we have covered all faults
        if fault_idx >= len(targets): 
            break
            
        # grab the next chunk of faults
        chunk = targets[fault_idx : fault_idx + FAULTS_PER_REG]
        fault_idx += len(chunk)
        
        # Prepare Variable Names
        xor_net_name = "n_obs_xor_{}".format(i)
        mux_name     = "MUX_OBS_{}".format(i)
        mux_out_net  = "n_mux_out_{}".format(i)

        # A. GENERATE LOGIC (XOR or Wire)
        logic_verilog = ""
        if len(chunk) == 1:
            # Single Fault: Direct connection (Buffer)
            logic_verilog = "  wire {} = {}; // Direct Observation\n".format(xor_net_name, chunk[0])
        else:
            # Multiple Faults: XOR Tree
            # Verilog: wire x = a ^ b ^ c;
            logic_verilog = "  wire {} = {}; // Compressed {} faults\n".format(xor_net_name, " ^ ".join(chunk), len(chunk))

        # B. GENERATE MUX
        # Mux Logic: Sel=0 -> Host.D (Normal), Sel=1 -> XOR_Net (Test)
        mux_verilog = "  wire {};\n".format(mux_out_net)
        mux_verilog += "  {} {} ( .{} ({}), .{} ({}), .{} (test_mode), .{} ({}) );\n".format(
            MUX_CELL_NAME, mux_name,
            MUX_PINS['A'], host['d_net'],     # Original Normal Data
            MUX_PINS['B'], xor_net_name,      # Compressed Fault Data
            MUX_PINS['S'],                    # Control
            MUX_PINS['Y'], mux_out_net        # Output to Register
        )

        # C. MODIFY NETLIST
        # Find the specific text of the register instance to replace
        reg_pattern = r"({}\s+{}\s*\([\s\S]*?)\.{}\s*\(\s*{}\s*\)([\s\S]*?\);)".format(
            re.escape(host['type']), re.escape(host['name']), 
            re.escape(host['d_pin']), re.escape(host['d_net'])
        )
        
        if re.search(reg_pattern, content):
            # 1. Update the Register's D-pin to listen to the MUX output
            content = re.sub(reg_pattern, r"\1.{}( {} )\3".format(host['d_pin'], mux_out_net), content, count=1)
            
            # 2. Insert the new Logic (XOR + MUX) immediately BEFORE the register
            # We find the start index of the register and insert our string there
            idx = content.find(host['type'] + " " + host['name'])
            content = content[:idx] + logic_verilog + mux_verilog + content[idx:]
            
            modified_count += 1
            print("      - Fixed: Reg '{}' now observing nets: {}".format(host['name'], chunk))
        else:
            print("      [!] Error: Could not locate register instance {}".format(host['name']))

    # 5. Write Output
    with open(OUTPUT_VERILOG, 'w') as f:
        f.write(content)
        
    print("--------------------------------------------------")
    print("  SUCCESS!")
    print("  - Modified {} registers.".format(modified_count))
    print("  - Total Observed Faults: {}".format(fault_idx))
    print("  - Output File: {}".format(OUTPUT_VERILOG))
    print("--------------------------------------------------")

if __name__ == "__main__":
    inject_compressed_observation()
