import re
import sys
from collections import defaultdict

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE = "b10.v"            
REPORT_FILE  = "stage1_failures.rpt" 
OUTPUT_TCL   = "insert_tpi_logic.tcl"
TOP_K_NODES  = 5                     
TRACE_DEPTH  = 3                     

# ==========================================
# PART 1: NETLIST PARSER
# ==========================================
class CircuitGraph:
    def __init__(self):
        self.drivers = defaultdict(list)
        self.instance_to_output = {}
        self.net_driver_inst = {}

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            content = f.read()

        # Regex for instances (Standard + Escaped Names)
        # Handles: DFFARX1_LVT \stato_reg[0] ( .D(...) );
        instance_pattern = re.compile(r'([\w\\]+)\s+([\w\\\[\]]+)\s*\((.*?)\);', re.DOTALL)
        
        matches = instance_pattern.findall(content)
        for cell_type, inst_name, pins in matches:
            clean_inst = inst_name.strip()
            
            # Parse Pins
            pin_pattern = re.compile(r'\.([\w\[\]]+)\s*\(\s*([\w\\\[\]]+)\s*\)')
            pin_matches = pin_pattern.findall(pins)
            
            inputs = []
            output = None
            
            for pin_name, net_name in pin_matches:
                # OUTPUT PINS
                if pin_name in ['Y', 'Z', 'Q', 'QN', 'SO']: 
                    output = net_name
                    self.instance_to_output[clean_inst] = net_name
                    self.net_driver_inst[net_name] = clean_inst
                
                # INPUT PINS
                elif pin_name in ['A', 'B', 'C', 'D', 'A1', 'A2', 'A3', 'A4', 
                                  'B1', 'B2', 'S0', 'S1', 'CLK', 'RSTB', 'SI', 'SE']:
                    inputs.append(net_name)
            
            if output:
                self.drivers[output] = inputs

        print "    - Parsed {} instances.".format(len(matches))

    def get_fanin_cone(self, start_inst, depth):
        cone_nodes = set()
        start_net = self.instance_to_output.get(start_inst)
        if not start_net: return cone_nodes

        def trace(current_net, current_depth):
            if current_depth == 0: return
            driver_nets = self.drivers.get(current_net, [])
            for d_net in driver_nets:
                driver_inst = self.net_driver_inst.get(d_net)
                if driver_inst:
                    cone_nodes.add(driver_inst)
                    trace(d_net, current_depth - 1)

        trace(start_net, depth)
        return cone_nodes

# ==========================================
# PART 2: FAILURE PARSER (FIXED)
# ==========================================
def parse_tetramax_failures(filename):
    print "[*] Parsing Failure Report: {}...".format(filename)
    victims = []
    
    with open(filename, 'r') as f:
        for line in f:
            # Skip empty lines or headers
            if len(line) < 5 or "defect" in line or "---" in line:
                continue

            # Look for Fault Codes: ND, AU, AN, AP, NO
            # Added "NO" because your report shows it.
            if any(c in line for c in ["ND", "AU", "AN", "AP", "NO"]):
                
                # ROBUST PARSING STRATEGY:
                # 1. Split line by whitespace to get columns
                parts = line.split()
                
                # 2. The Path is usually the last column (e.g., U145/Y)
                if len(parts) > 0:
                    path = parts[-1]
                    
                    # 3. Extract Instance Name (Left of the /)
                    if "/" in path:
                        inst = path.split('/')[0]
                        victims.append(inst)

    victims = list(set(victims))
    print "    - Found {} unique RPR victim nodes.".format(len(victims))
    return victims

# ==========================================
# PART 3: INTERSECTION HEURISTIC
# ==========================================
def run_intersection_heuristic(circuit, victims):
    print "[*] Running Structural Cone Analysis..."
    node_scores = defaultdict(int)
    
    for victim in victims:
        cone = circuit.get_fanin_cone(victim, TRACE_DEPTH)
        for node in cone:
            node_scores[node] += 1
        node_scores[victim] += 1 

    sorted_nodes = sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_nodes[:TOP_K_NODES]

# ==========================================
# PART 4: TCL GENERATION
# ==========================================
def generate_tcl_script(selected_nodes, circuit):
    print "[*] Generating TCL Script: {}...".format(OUTPUT_TCL)
    with open(OUTPUT_TCL, 'w') as f:
        f.write("# Stage 1: Inversion TPI Insertion\n")
        f.write("create_port -direction in TEST_ENABLE\n\n")
        
        for node, score in selected_nodes:
            f.write("# Node: {} (Score: {})\n".format(node, score))
            
            # --- INTELLIGENT PIN SELECTION ---
            # Ask the Circuit Graph: "What is the output pin name for this instance?"
            output_pin = circuit.instance_to_output.get(node)
            
            # Fallback if the parser missed it (Default to Y for gates, Q for Regs)
            if not output_pin:
                if "reg" in node or "DFF" in node:
                    output_pin = "n1" # Danger! It's better to look up the specific net.
                    # Actually, reliance on the parser dictionary is best.
            
            # Since our parser stored the Output NET, we need to find the Output PIN NAME.
            # We can just assume Q for registers and Y for gates based on the name.
            if "reg" in node or "last_" in node: 
                pin_name = "Q"
            else:
                pin_name = "Y"
                
            # Clean up names for the new cell
            new_cell = "TPI_XOR_{}".format(node).replace("\\", "").replace("[", "_").replace("]", "_")
            
            # Write the correct command
            f.write("insert_buffer {}/{} {} -lib_cell XOR2X1_LVT\n".format(node, pin_name, new_cell))
            f.write("connect_net TEST_ENABLE {}/A2\n".format(new_cell)) 
            f.write("\n")
    print "[*] Done."

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    circuit = CircuitGraph()
    circuit.parse_verilog(NETLIST_FILE)
    
    victims = parse_tetramax_failures(REPORT_FILE)
    if victims:
        top_nodes = run_intersection_heuristic(circuit, victims)
        print "\n[RESULT] Top Selected Nodes:"
        for node, score in top_nodes:
            print "  - {}: Covers {} faults".format(node, score)
        generate_tcl_script(top_nodes)
    else:
        print "Error: No victims found. The regex/parsing logic might still be mismatched."

