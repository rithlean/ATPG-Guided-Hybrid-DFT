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
                # OUTPUT PINS (Mapped to Y or Q/QN)
                if pin_name in ['Y', 'Z', 'Q', 'QN', 'SO']: 
                    output = net_name
                    # Key Step: Save exactly which pin is the output for this instance
                    # We store the pin NAME (e.g., 'Q') separately if we wanted, 
                    # but here we map Instance -> Net.
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
# PART 2: FAILURE PARSER
# ==========================================
def parse_tetramax_failures(filename):
    print "[*] Parsing Failure Report: {}...".format(filename)
    victims = []
    
    with open(filename, 'r') as f:
        for line in f:
            if len(line) < 5 or "defect" in line or "---" in line:
                continue

            if any(c in line for c in ["ND", "AU", "AN", "AP", "NO"]):
                parts = line.split()
                if len(parts) > 0:
                    path = parts[-1]
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
# PART 4: TCL GENERATION (FIXED)
# ==========================================
def generate_tcl_script(selected_nodes, circuit):
    print "[*] Generating TCL Script: {}...".format(OUTPUT_TCL)
    with open(OUTPUT_TCL, 'w') as f:
        f.write("# Stage 1: Inversion TPI Insertion\n")
        f.write("create_port -direction in TEST_ENABLE\n\n")
        
        for node, score in selected_nodes:
            f.write("# Node: {} (Score: {})\n".format(node, score))
            
            # INTELLIGENT PIN SELECTION
            # 1. Try to guess based on name (Robust Fallback)
            if "reg" in node or "last_" in node or "DFF" in node:
                pin_name = "Q"
            else:
                pin_name = "Y"
            
            # 2. Try to look up the Net to be sure (Advanced)
            # This part is optional but good for debugging
            # driven_net = circuit.instance_to_output.get(node)
            
            # Clean up names for the new cell (remove illegal chars for TCL variables)
            new_cell = "TPI_XOR_{}".format(node).replace("\\", "").replace("[", "_").replace("]", "_")
            
            # Write the insertion command
            # Note: We keep the backslash in the Node Name for DC command, but clean it for the new cell name
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
            
        # FIXED: Passing both arguments now
        generate_tcl_script(top_nodes, circuit)
    else:
        print "Error: No victims found."
