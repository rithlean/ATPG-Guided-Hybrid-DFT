import re
import sys
from collections import defaultdict

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE    = "b10_tpi.v"      # Input Netlist (Result of Stage 1)
REPORT_FILE     = "stage2_failures.rpt" # Fault report from Stage 1 run
OUTPUT_VERILOG  = "b10_scan.v"     # Output Netlist

# SCAN CONFIGURATION
MUX_CELL_NAME   = "MUX2X1_LVT"     # Name of your Library MUX
MUX_PINS        = {"A": "A", "B": "B", "S": "S", "Y": "Y"} 
# Mapping: A=Normal Data, B=Scan Data, S=Scan Enable, Y=Output

MAX_SCAN_LENGTH = 5                # How many FFs to scan (Budget)

# ==========================================
# PART 1: NETLIST PARSER (Enhanced for Registers)
# ==========================================
class CircuitGraph:
    def __init__(self):
        self.drivers = defaultdict(list)
        self.instance_to_output = {}
        self.net_driver_inst = {}
        self.inst_type = {} 
        self.inst_pins = defaultdict(dict) # Stores pin connections: {inst: {pin: net}}

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            self.raw_content = f.read() # Keep raw text for rewriting later

        # Regex to find instances and capture their pin lists
        instance_pattern = re.compile(r'([\w\\]+)\s+([\w\\\[\]]+)\s*\((.*?)\);', re.DOTALL)
        matches = instance_pattern.findall(self.raw_content)
        
        for cell_type, inst_name, pins in matches:
            clean_inst = inst_name.strip()
            self.inst_type[clean_inst] = cell_type
            
            # Parse Pins: .Pin(Net)
            pin_pattern = re.compile(r'\.([\w\[\]]+)\s*\(\s*([\w\\\[\]]+)\s*\)')
            pin_matches = pin_pattern.findall(pins)
            
            inputs = []
            output = None
            
            for pin_name, net_name in pin_matches:
                self.inst_pins[clean_inst][pin_name] = net_name # Store pin map
                
                # Identify Outputs (Q/QN for Regs, Y/Z for Gates)
                if pin_name in ['Y', 'Z', 'Q', 'QN']: 
                    output = net_name
                    self.instance_to_output[clean_inst] = net_name
                    self.net_driver_inst[net_name] = clean_inst
                # Identify Inputs
                elif pin_name not in ['CLK', 'RSTB', 'VDD', 'VSS']:
                    inputs.append(net_name)
            
            if output:
                self.drivers[output] = inputs

        print "    - Parsed {} instances.".format(len(matches))

    # BFS Cone Trace (Same as Stage 1)
    def get_full_fanin_cone(self, start_inst):
        cone_map = {} 
        start_net = self.instance_to_output.get(start_inst)
        if not start_net: return {}
        
        driver_nets = self.drivers.get(start_net, [])
        bfs_queue = [] 
        
        for net in driver_nets:
             d_inst = self.net_driver_inst.get(net)
             if d_inst: bfs_queue.append( (d_inst, 1) )
        
        visited_insts = set()
        while bfs_queue:
            curr_inst, dist = bfs_queue.pop(0)
            if curr_inst in visited_insts: continue
            visited_insts.add(curr_inst)
            
            cone_map[curr_inst] = dist
            
            # Boundary check: Stop at Registers
            ctype = self.inst_type.get(curr_inst, "")
            if "reg" in curr_inst or "DFF" in ctype: 
                continue
                
            curr_net = self.instance_to_output.get(curr_inst)
            if curr_net:
                upstream_nets = self.drivers.get(curr_net, [])
                for u_net in upstream_nets:
                    u_inst = self.net_driver_inst.get(u_net)
                    if u_inst and u_inst not in visited_insts:
                        bfs_queue.append( (u_inst, dist + 1) )
        return cone_map

# ==========================================
# PART 2: FAILURE PARSER (Unchanged)
# ==========================================
def parse_tetramax_failures(filename):
    print "[*] Parsing Failure Report: {}...".format(filename)
    victims = []
    try:
        with open(filename, 'r') as f:
            for line in f:
                if "ND" in line or "NO" in line:
                    parts = line.split()
                    if len(parts) > 0:
                        path = parts[-1]
                        inst = path.split('/')[0]
                        victims.append(inst)
    except IOError:
        return []
    return list(set(victims))

# ==========================================
# PART 3: REGISTER SELECTION
# ==========================================
def select_scan_candidates(circuit, victims):
    print "[*] Analyzing Registers for Partial Scan..."
    
    # We only care about Registers that appear in the cone of failures
    reg_scores = defaultdict(float)
    
    for victim in victims:
        # 1. If victim IS a register, prioritize it heavily
        if "reg" in victim or "DFF" in circuit.inst_type.get(victim, ""):
            reg_scores[victim] += 10.0 # Huge weight for direct failures
        
        # 2. Trace back to find which registers drive the logic failing downstream
        cone = circuit.get_full_fanin_cone(victim)
        for node, dist in cone.items():
            if "reg" in node or "DFF" in circuit.inst_type.get(node, ""):
                # Closer registers get higher score
                weight = 5.0 / (1.0 + dist)
                reg_scores[node] += weight

    # Sort registers by score
    sorted_regs = sorted(reg_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Pick top N
    selected = []
    print "    ---------------------------------------"
    print "    Rank | Register Name      | Score"
    print "    ---------------------------------------"
    for i, (reg, score) in enumerate(sorted_regs):
        if i >= MAX_SCAN_LENGTH: break
        print "    {:4} | {:18} | {:.2f}".format(i+1, reg, score)
        selected.append(reg)
        
    return selected

# ==========================================
# PART 4: VERILOG REWRITER (The Hard Part)
# ==========================================
def write_scan_verilog(circuit, scan_chain):
    print "[*] Injecting Scan Chain into Verilog..."
    
    content = circuit.raw_content
    
    # 1. ADD PORTS (Naive insertion after last input/output)
    # Find the last semicolon before the first 'assign' or 'instance'
    # A simple hack: look for the module definition end ");" or just add inputs
    if "input" in content:
        # Add scan ports after the first input found
        new_ports = "\n  input scan_in, scan_enable;\n  output scan_out;\n"
        content = content.replace("input", new_ports + "input", 1)
    
    # 2. BUILD THE CHAIN
    # Chain: scan_in -> [MUX -> REG1] -> Q1 -> [MUX -> REG2] -> Q2 ... -> scan_out
    
    current_scan_input = "scan_in"
    
    for i, reg_name in enumerate(scan_chain):
        is_last = (i == len(scan_chain) - 1)
        
        # Get details
        reg_type = circuit.inst_type[reg_name]
        pins = circuit.inst_pins[reg_name]
        
        original_d_net = pins.get('D') # Assumes D pin is named 'D'
        q_net = pins.get('Q')          # Assumes Q pin is named 'Q'
        
        if not original_d_net or not q_net:
            print "WARNING: Could not find D/Q pins for {}. Skipping.".format(reg_name)
            continue

        # Create Names
        mux_inst_name = "MUX_SCAN_{}".format(i)
        mux_out_net   = "n_scan_mux_{}".format(i)
        
        # 3. CREATE MUX INSTANCE STRING
        # MUX Logic: If S=0 (Func), Y=A. If S=1 (Scan), Y=B.
        # .A(func), .B(scan), .S(se)
        mux_verilog = "  wire {};\n".format(mux_out_net)
        mux_verilog += "  {} {} ( .{} ({}), .{} ({}), .{} (scan_enable), .{} ({}) );\n".format(
            MUX_CELL_NAME, mux_inst_name,
            MUX_PINS['A'], original_d_net,     # Normal Path
            MUX_PINS['B'], current_scan_input, # Scan Path
            MUX_PINS['S'],                     # Scan Enable
            MUX_PINS['Y'], mux_out_net         # Result
        )
        
        # 4. MODIFY REGISTER INSTANCE IN TEXT
        # We need to find the specific text ".D( old_net )" for THIS instance and replace it.
        # Strategy: Find the instance definition, then replace the D connection inside it.
        
        # Regex to match the specific instance: "DFF ... reg_name ... ( ... );"
        # We use re.escape to handle brackets in names like reg[0]
        reg_pattern = r"({}\s+{}\s*\([\s\S]*?)\.D\s*\(\s*{}\s*\)([\s\S]*?\);)".format(
            re.escape(reg_type), re.escape(reg_name), re.escape(original_d_net)
        )
        
        # Replacement: Keep start and end, swap the D net
        replacement = r"\1.D( {} )\2".format(mux_out_net)
        
        # Perform Substitution
        content = re.sub(reg_pattern, replacement, content, count=1)
        
        # Insert the MUX definition BEFORE the register
        # We find the register again (now modified) and prepend the MUX
        idx = content.find(reg_type + " " + reg_name)
        if idx != -1:
            content = content[:idx] + mux_verilog + content[idx:]
        
        # Update scan input for next loop
        current_scan_input = q_net
        
        # If last, connect output port
        if is_last:
            content += "\n  assign scan_out = {};\n".format(q_net)

    # 5. WRITE FILE
    with open(OUTPUT_VERILOG, 'w') as f:
        f.write(content)
    print "[*] Success! Scan Chain inserted. Output: {}".format(OUTPUT_VERILOG)

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    circuit = CircuitGraph()
    circuit.parse_verilog(NETLIST_FILE)
    victims = parse_tetramax_failures(REPORT_FILE)
    
    if victims:
        scan_chain = select_scan_candidates(circuit, victims)
        if scan_chain:
            write_scan_verilog(circuit, scan_chain)
    else:
        print "Error: No victims found to guide scan insertion."
