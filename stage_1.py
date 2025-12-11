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

    # --- THIS WAS MISSING IN THE PREVIOUS PASTE ---
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
    # -----------------------------------------------

# ==========================================
# PART 2: FAILURE PARSER
# ==========================================
def parse_tetramax_failures(filename):
    print "[*] Parsing Failure Report: {}...".format(filename)
    victims = []
    with open(filename, 'r') as f:
        for line in f:
            if len(line) < 5 or "defect" in line or "---" in line: continue
            if any(c in line for c in ["ND", "AU", "AN", "AP", "NO"]):
                parts = line.split()
                if len(parts) > 0:
                    path = parts[-1]
                    if "/" in path:
                        inst = path.split('/')[0]
                        victims.append(inst)
    return list(set(victims))

# ==========================================
# PART 3: INTERSECTION HEURISTIC
# ==========================================
def run_intersection_heuristic(circuit, victims):
    print "[*] Running Structural Cone Analysis..."
    node_scores = defaultdict(int)
    for victim in victims:
        cone = circuit.get_fanin_cone(victim, TRACE_DEPTH)
        for node in cone:
            # FILTER: Skip Global Reset Driver (U115)
            if node == "U115": continue 
            node_scores[node] += 1
        if victim != "U115": node_scores[victim] += 1 

    sorted_nodes = sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_nodes[:TOP_K_NODES]

# ==========================================
# PART 4: TCL GENERATION
# ==========================================
def generate_tcl_script(selected_nodes, circuit):
    print "[*] Generating TCL Script: {}...".format(OUTPUT_TCL)
    with open(OUTPUT_TCL, 'w') as f:
        f.write("# Stage 1: Inversion TPI Insertion (ECO Surgery Mode)\n")
        f.write("# Robust Syntax: Uses braces and dynamic lib_cell lookup\n\n")
        
        # 1. SETUP: Find the exact library cell name once
        f.write("# Find the XOR cell in the loaded library to avoid ambiguity\n")
        f.write("set lib_cell_ref [get_object_name [get_lib_cells */XOR2X1_LVT]]\n")
        f.write("if {$lib_cell_ref == \"\"} { echo \"Error: XOR2X1_LVT not found in library!\"; exit }\n")
        f.write("echo \"Using Library Cell: $lib_cell_ref\"\n\n")
        
        # 2. SETUP: Create Port AND Net
        f.write("create_port -direction in TEST_ENABLE\n")
        f.write("create_net TEST_ENABLE\n")
        f.write("connect_net TEST_ENABLE TEST_ENABLE\n\n")
        
        for node, score in selected_nodes:
            f.write("# --------------------------------------------------------\n")
            f.write("# Target Node: {} (Score: {})\n".format(node, score))
            
            # Determine Pin Name
            if "reg" in node or "last_" in node or "DFF" in node:
                pin_name = "Q"
            else:
                pin_name = "Y"
                
            # Construct Safe Names
            full_pin_path = "{" + "{}/{}".format(node, pin_name) + "}"
            clean_name = node.replace("\\", "").replace("[", "_").replace("]", "_")
            xor_inst_name = "TPI_XOR_{}".format(clean_name)
            new_net_name  = "n_tpi_{}".format(clean_name)

            # --- ECO COMMANDS ---
            f.write("set target_net [get_nets -of_objects [get_pins {}]]\n".format(full_pin_path))
            f.write("create_cell {{{}}} $lib_cell_ref\n".format(xor_inst_name))
            f.write("disconnect_net $target_net {}\n".format(full_pin_path))
            f.write("connect_net $target_net {}/Y\n".format(xor_inst_name))
            f.write("create_net {}\n".format(new_net_name))
            f.write("connect_net {} {}\n".format(new_net_name, full_pin_path))
            f.write("connect_net {} {}/A1\n".format(new_net_name, xor_inst_name))
            f.write("connect_net TEST_ENABLE {}/A2\n".format(xor_inst_name))
            f.write("\n")
            
    print "[*] Done. Generated Robust ECO commands."

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    circuit = CircuitGraph()
    circuit.parse_verilog(NETLIST_FILE)
    victims = parse_tetramax_failures(REPORT_FILE)
    if victims:
        top_nodes = run_intersection_heuristic(circuit, victims)
        generate_tcl_script(top_nodes, circuit)
    else:
        print "Error: No victims found."
