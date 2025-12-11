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
# PART 1: NETLIST PARSER (Standard)
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

        instance_pattern = re.compile(r'([\w\\]+)\s+([\w\\\[\]]+)\s*\((.*?)\);', re.DOTALL)
        matches = instance_pattern.findall(content)
        for cell_type, inst_name, pins in matches:
            clean_inst = inst_name.strip()
            pin_pattern = re.compile(r'\.([\w\[\]]+)\s*\(\s*([\w\\\[\]]+)\s*\)')
            pin_matches = pin_pattern.findall(pins)
            
            inputs = []
            output = None
            
            for pin_name, net_name in pin_matches:
                if pin_name in ['Y', 'Z', 'Q', 'QN', 'SO']: 
                    output = net_name
                    self.instance_to_output[clean_inst] = net_name
                    self.net_driver_inst[net_name] = clean_inst
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
# PART 2: FAILURE PARSER (Standard)
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
# PART 3: INTERSECTION HEURISTIC (Standard)
# ==========================================
# In stage_1.py, update this function:

def run_intersection_heuristic(circuit, victims):
    print "[*] Running Structural Cone Analysis..."
    node_scores = defaultdict(int)
    
    for victim in victims:
        cone = circuit.get_fanin_cone(victim, TRACE_DEPTH)
        for node in cone:
            # --- FILTER ADDED HERE ---
            # If the node is U115 (Reset Driver), SKIP IT.
            # We skip it because messing with Global Reset is dangerous.
            if node == "U115": 
                continue 
            
            node_scores[node] += 1
        
        # Don't count the victim itself if it is U115
        if victim != "U115":
            node_scores[victim] += 1 

    sorted_nodes = sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_nodes[:TOP_K_NODES]

# ==========================================
# PART 4: TCL GENERATION (CRITICAL UPDATE)
# ==========================================
def generate_tcl_script(selected_nodes, circuit):
    print "[*] Generating TCL Script: {}...".format(OUTPUT_TCL)
    with open(OUTPUT_TCL, 'w') as f:
        f.write("# Stage 1: Inversion TPI Insertion (ECO Surgery Mode)\n")
        f.write("# This script manually splices XOR gates into the netlist.\n")
        f.write("create_port -direction in TEST_ENABLE\n\n")
        
        for node, score in selected_nodes:
            f.write("# --------------------------------------------------------\n")
            f.write("# Target Node: {} (Score: {})\n".format(node, score))
            
            # 1. Determine Pin Name (Q for regs, Y for gates)
            if "reg" in node or "last_" in node or "DFF" in node:
                pin_name = "Q"
            else:
                pin_name = "Y"
                
            # 2. Create Unique Names for New Logic
            # Clean up the node name to make it a valid TCL variable
            clean_name = node.replace("\\", "").replace("[", "_").replace("]", "_")
            xor_inst_name = "TPI_XOR_{}".format(clean_name)
            new_net_name  = "n_tpi_{}".format(clean_name)

            # 3. WRITE THE ECO SURGERY COMMANDS
            # Step A: Identify the existing net connected to the pin
            # We use 'get_nets -of_objects' to find what wire is currently there.
            f.write("set target_net [get_nets -of_objects [get_pins {}/{}]]\n".format(node, pin_name))
            
            # Step B: Create the new XOR Cell (Floating)
            f.write("create_cell {} XOR2X1_LVT\n".format(xor_inst_name))
            
            # Step C: Disconnect the existing net from the driver pin
            # This leaves the driver pin empty and the net floating (connected to loads)
            f.write("disconnect_net $target_net {}/{}\n".format(node, pin_name))
            
            # Step D: Connect the existing net (Loads) to the XOR Output (Y)
            f.write("connect_net $target_net {}/Y\n".format(xor_inst_name))
            
            # Step E: Create a new tiny net to connect Driver -> XOR Input (A1)
            f.write("create_net {}\n".format(new_net_name))
            f.write("connect_net {} {}/{}\n".format(new_net_name, node, pin_name))
            f.write("connect_net {} {}/A1\n".format(new_net_name, xor_inst_name))
            
            # Step F: Connect Control Signal to XOR Input (A2)
            f.write("connect_net TEST_ENABLE {}/A2\n".format(xor_inst_name))
            f.write("\n")
            
    print "[*] Done. Generated robust ECO commands."

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
            
        generate_tcl_script(top_nodes, circuit)
    else:
        print "Error: No victims found."
