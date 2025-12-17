import re
import sys
from collections import defaultdict

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE    = "b10_tpi.v"            # Input: The TPI-inserted netlist from Stage 1
REPORT_FILE     = "stage2_failures.rpt"  # Input: The fault report after running Stage 1
OUTPUT_VERILOG  = "b10_scan.v"           # Output: Final Scan-Inserted Netlist

# SCAN CONFIGURATION
MUX_CELL_NAME   = "MUX2X1_LVT"           # Check your library for the exact MUX name!
MUX_PINS        = {"A": "A", "B": "B", "S": "S", "Y": "Y"} 
# Mapping: A=Normal Data, B=Scan Data, S=Scan Enable, Y=Output

MAX_SCAN_LENGTH = 5                      # Budget: How many registers to scan

# INTELLIGENT SELECTION FILTERS
# We ignore these registers because we already fixed them with XOR TPIs (Stage 1).
# This forces the tool to select Data Path registers (High Observability points).
IGNORE_PATTERNS = ["stato_reg", "TPI_XOR"] 

# ==========================================
# PART 1: NETLIST PARSER
# ==========================================
class CircuitGraph:
    def __init__(self):
        self.drivers = defaultdict(list)
        self.instance_to_output = {}
        self.net_driver_inst = {}
        self.inst_type = {} 
        self.inst_pins = defaultdict(dict)
        self.raw_content = ""

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            self.raw_content = f.read()

        # Regex to find instances and capture their pin lists
        instance_pattern = re.compile(r'([\w\\]+)\s+([\w\\\[\]]+)\s*\((.*?)\);', re.DOTALL)
        matches = instance_pattern.findall(self.raw_content)
        
        for cell_type, inst_name, pins in matches:
            clean_inst = inst_name.strip()
            self.inst_type[clean_inst] = cell_type
            
            # Parse Pins
            pin_pattern = re.compile(r'\.([\w\[\]]+)\s*\(\s*([\w\\\[\]]+)\s*\)')
            pin_matches = pin_pattern.findall(pins)
            
            for pin_name, net_name in pin_matches:
                self.inst_pins[clean_inst][pin_name] = net_name 
                
                # Identify Outputs (Q/QN for Regs, Y/Z for Gates)
                if pin_name in ['Y', 'Z', 'Q', 'QN']: 
                    self.instance_to_output[clean_inst] = net_name
                    self.net_driver_inst[net_name] = clean_inst
                    # Initialize list for this net
                    if net_name not in self.drivers: self.drivers[net_name] = []
                
                # Identify Inputs (Anything not Output or Power)
                elif pin_name not in ['CLK', 'RSTB', 'VDD', 'VSS', 'CK', 'RN']:
                    # We need to map NET -> DRIVER, so we store inputs relative to output
                    # But for simple tracing, we just need to know inputs of this instance
                    pass
            
            # Map Inputs to Output for tracing
            output_net = self.instance_to_output.get(clean_inst)
            if output_net:
                for pin_name, net_name in pin_matches:
                    if pin_name not in ['Y', 'Z', 'Q', 'QN', 'CLK', 'RSTB', 'VDD', 'VSS']:
                        self.drivers[output_net].append(net_name)

        print "    - Parsed {} instances.".format(len(matches))

    def get_full_fanin_cone(self, start_inst):
        """ Traces backwards to find which logic drives the fault. """
        cone_map = {} 
        start_net = self.instance_to_output.get(start_inst)
        if not start_net: return {}
        
        # Initial drivers
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
            
            # Boundary check: Stop at Registers (Don't trace through them)
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
# PART 2: FAILURE PARSER
# ==========================================
def parse_tetramax_failures(filename):
    print "[*] Parsing Failure Report: {}...".format(filename)
    victims = []
    try:
        with open(filename, 'r') as f:
            for line in f:
                # We specifically look for NO (Not Observed) and ND (Not Detected)
                if "ND" in line or "NO" in line:
                    parts = line.split()
                    if len(parts) > 0:
                        path = parts[-1]
                        inst = path.split('/')[0]
                        victims.append(inst)
    except IOError:
        print "Error: Could not read report file."
        return []
    return list(set(victims))

# ==========================================
# PART 3: SMART SELECTION
# ==========================================
def select_scan_candidates(circuit, victims):
    print "[*] Analyzing Logic for Partial Scan..."
    print "    - Ignoring Control Logic: {}".format(IGNORE_PATTERNS)
    
    reg_scores = defaultdict(float)
    
    for victim in victims:
        # 1. If the victim IS a register, it's an Observability Point.
        # This is the "Sink" logic. If last_r_reg is NO, we must scan it.
        if "reg" in victim or "DFF" in circuit.inst_type.get(victim, ""):
            reg_scores[victim] += 10.0 
        
        # 2. Trace back to see if a register drives this fault
        cone = circuit.get_full_fanin_cone(victim)
        for node, dist in cone.items():
            if "reg" in node or "DFF" in circuit.inst_type.get(node, ""):
                weight = 5.0 / (1.0 + dist)
                reg_scores[node] += weight

    # Sort registers by score
    sorted_regs = sorted(reg_scores.items(), key=lambda x: x[1], reverse=True)
    
    selected = []
    print "    ---------------------------------------"
    print "    Rank | Register Name      | Score"
    print "    ---------------------------------------"
    
    for i, (reg, score) in enumerate(sorted_regs):
        # FILTER: Skip Control Logic or previously fixed logic
        if any(pat in reg for pat in IGNORE_PATTERNS):
            continue
            
        if len(selected) >= MAX_SCAN_LENGTH: break
        
        print "    {:4} | {:18} | {:.2f}".format(i+1, reg, score)
        selected.append(reg)
        
    return selected

# ==========================================
# PART 4: VERILOG SCAN INSERTION
# ==========================================
def write_scan_verilog(circuit, scan_chain):
    print "[*] Injecting Scan Chain into Verilog..."
    
    content = circuit.raw_content
    
    # 1. ADD PORTS
    if "input" in content:
        # Check if ports already exist (to avoid duplication if re-running)
        if "scan_in" not in content:
            new_ports = "\n  input scan_in, scan_enable;\n  output scan_out;\n"
            content = content.replace("input", new_ports + "input", 1)
    
    # 2. BUILD THE CHAIN
    current_scan_input = "scan_in"
    
    for i, reg_name in enumerate(scan_chain):
        is_last = (i == len(scan_chain) - 1)
        
        reg_type = circuit.inst_type[reg_name]
        pins = circuit.inst_pins[reg_name]
        
        # Identify Pins (Adapt for your library naming D/Q/QN)
        original_d_net = pins.get('D') or pins.get('d')
        q_net = pins.get('Q') or pins.get('q') or pins.get('QN') or pins.get('qn')
        
        if not original_d_net or not q_net:
            print "WARNING: Could not find D/Q pins for {}. Skipping.".format(reg_name)
            continue

        mux_inst_name = "MUX_SCAN_{}".format(i)
        mux_out_net   = "n_scan_mux_{}".format(i)
        
        # Create MUX: .A(Func), .B(Scan), .S(Enable) -> Y
        mux_verilog = "  wire {};\n".format(mux_out_net)
        mux_verilog += "  {} {} ( .{} ({}), .{} ({}), .{} (scan_enable), .{} ({}) );\n".format(
            MUX_CELL_NAME, mux_inst_name,
            MUX_PINS['A'], original_d_net,     
            MUX_PINS['B'], current_scan_input, 
            MUX_PINS['S'],                     
            MUX_PINS['Y'], mux_out_net         
        )
        
        # Regex Replace the Register Connection
        reg_pattern = r"({}\s+{}\s*\([\s\S]*?)\.D\s*\(\s*{}\s*\)([\s\S]*?\);)".format(
            re.escape(reg_type), re.escape(reg_name), re.escape(original_d_net)
        )
        
        # Verify match before replacing
        if not re.search(reg_pattern, content):
            print "    ! Error matching regex for {}".format(reg_name)
            continue

        replacement = r"\1.D( {} )\2".format(mux_out_net)
        content = re.sub(reg_pattern, replacement, content, count=1)
        
        # Insert MUX definition before the register
        idx = content.find(reg_type + " " + reg_name)
        if idx != -1:
            content = content[:idx] + mux_verilog + content[idx:]
        
        current_scan_input = q_net
        
        if is_last:
            content += "\n  assign scan_out = {};\n".format(q_net)

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
            print "No suitable registers found after filtering."
    else:
        print "Error: No victims found."
