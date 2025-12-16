import re
import sys
from collections import defaultdict

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE    = "b10.v"            
REPORT_FILE     = "stage1_failures.rpt" 
OUTPUT_TCL      = "insert_tpi_logic.tcl"

# PURE ANALYSIS PARAMETERS (No arbitrary depths)
# ------------------------------------------------
# ELBOW_THRESHOLD: The minimum relative impact required to select a node.
# If a node has < 10% of the impact of the top node, we stop.
ELBOW_THRESHOLD = 0.10  
MAX_AREA_BUDGET = 5    # Hard engineering limit (safety net)

# ==========================================
# PART 1: NETLIST PARSER
# ==========================================
class CircuitGraph:
    def __init__(self):
        self.drivers = defaultdict(list)
        self.instance_to_output = {}
        self.net_driver_inst = {}
        self.inst_type = {} # To detect Registers vs Gates

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            content = f.read()

        # Capture instance type to identify registers later
        instance_pattern = re.compile(r'([\w\\]+)\s+([\w\\\[\]]+)\s*\((.*?)\);', re.DOTALL)
        matches = instance_pattern.findall(content)
        
        for cell_type, inst_name, pins in matches:
            clean_inst = inst_name.strip()
            self.inst_type[clean_inst] = cell_type # Store type (e.g., DFF)
            
            pin_pattern = re.compile(r'\.([\w\[\]]+)\s*\(\s*([\w\\\[\]]+)\s*\)')
            pin_matches = pin_pattern.findall(pins)
            
            inputs = []
            output = None
            
            for pin_name, net_name in pin_matches:
                # Identify Outputs
                if pin_name in ['Y', 'Z', 'Q', 'QN', 'SO']: 
                    output = net_name
                    self.instance_to_output[clean_inst] = net_name
                    self.net_driver_inst[net_name] = clean_inst
                # Identify Inputs
                elif pin_name in ['A', 'B', 'C', 'D', 'A1', 'A2', 'A3', 'A4', 
                                  'B1', 'B2', 'S0', 'S1', 'CLK', 'RSTB', 'SI', 'SE', 'TE']:
                    inputs.append(net_name)
            
            if output:
                self.drivers[output] = inputs
        print "    - Parsed {} instances.".format(len(matches))

    # ---------------------------------------------------------
    # NEW: Distance-Aware Cone Trace (BFS)
    # ---------------------------------------------------------
    def get_full_fanin_cone(self, start_inst):
        """
        Traces backwards until a Register or Primary Input is hit.
        Returns a dictionary: {node_name: distance_from_fault}
        """
        cone_map = {} # Stores {node: min_distance}
        
        start_net = self.instance_to_output.get(start_inst)
        if not start_net: return {}
        
        driver_nets = self.drivers.get(start_net, [])
        
        # Standard BFS Queue: [(Instance, Distance)]
        bfs_queue = [] 
        
        # Initialize queue with immediate drivers of the fault
        for net in driver_nets:
             d_inst = self.net_driver_inst.get(net)
             if d_inst:
                 bfs_queue.append( (d_inst, 1) )
        
        visited_insts = set()

        while bfs_queue:
            curr_inst, dist = bfs_queue.pop(0)
            
            if curr_inst in visited_insts: continue
            visited_insts.add(curr_inst)
            
            # Record the node and its distance
            cone_map[curr_inst] = dist
            
            # CHECK BOUNDARY: If it's a Flip-Flop, STOP tracing this path
            # (Assumes library cells like DFF*, SDFF*, etc.)
            ctype = self.inst_type.get(curr_inst, "")
            if "DFF" in ctype or "reg" in ctype or "LA" in ctype: 
                continue
                
            # Else, keep going deeper
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
                if len(line) < 5 or "defect" in line or "---" in line: continue
                if any(c in line for c in ["ND", "AU", "AN", "AP", "NO"]):
                    parts = line.split()
                    if len(parts) > 0:
                        path = parts[-1]
                        if "/" in path:
                            inst = path.split('/')[0]
                            victims.append(inst)
    except IOError:
        print "Error: Could not read report file."
        return []
    return list(set(victims))

# ==========================================
# PART 3: WEIGHTED ANALYSIS & ELBOW SELECTION
# ==========================================
def run_weighted_analysis(circuit, victims):
    print "[*] Running Distance-Weighted Topological Analysis..."
    
    # 1. Calculate Weighted Scores
    node_scores = defaultdict(float)
    
    for victim in victims:
        # Get map of {node: distance}
        cone_map = circuit.get_full_fanin_cone(victim)
        
        for node, distance in cone_map.items():
            # FILTER: Skip Global Reset Driver
            if node == "U115": continue 
            
            # FORMULA: Score += 1 / (1 + distance)
            weight = 1.0 / (1.0 + float(distance))
            node_scores[node] += weight
            
        # Add the victim itself (Distance 0 -> Weight 1.0)
        if victim != "U115": 
            node_scores[victim] += 1.0

    # 2. Sort Nodes
    sorted_nodes = sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
    
    if not sorted_nodes: return []

    # 3. ELBOW POINT SELECTION (The "Pure Analysis" Logic)
    print "[*] Performing Knee-Point Selection (Threshold: {})...".format(ELBOW_THRESHOLD)
    selected_nodes = []
    
    # Peak score is the reference
    max_score = sorted_nodes[0][1]
    
    print "    Rank | Node       | Score  | Normalized"
    print "    ---------------------------------------"
    
    for i in range(len(sorted_nodes)):
        node, score = sorted_nodes[i]
        
        # Calculate Normalized Impact (0.0 to 1.0)
        norm_score = score / max_score
        
        # Debug Print
        if i < 10: 
            print "    {:4} | {:10} | {:6.2f} | {:4.2f}".format(i+1, node, score, norm_score)
        
        # STOPPING CRITERIA:
        # 1. Hard Engineering Limit (Budget)
        if len(selected_nodes) >= MAX_AREA_BUDGET:
            print "    -> Stopped: Hit Max Area Budget ({})".format(MAX_AREA_BUDGET)
            break
            
        # 2. Diminishing Returns (Elbow)
        # If this node has less than 10% of the impact of the best node, stop.
        if norm_score < ELBOW_THRESHOLD:
            print "    -> Stopped: Hit Diminishing Returns (Score < {}% of Peak)".format(ELBOW_THRESHOLD*100)
            break
            
        selected_nodes.append((node, score))
        
    return selected_nodes

# ==========================================
# PART 4: TCL GENERATION (Unchanged Logic)
# ==========================================
def generate_tcl_script(selected_nodes, circuit):
    print "[*] Generating TCL Script: {}...".format(OUTPUT_TCL)
    with open(OUTPUT_TCL, 'w') as f:
        f.write("# Stage 1: Inversion TPI Insertion\n")
        f.write("set lib_cell_ref [get_object_name [get_lib_cells */XOR2X1_LVT]]\n")
        f.write("if {$lib_cell_ref == \"\"} { echo \"Error: XOR2X1_LVT not found!\"; exit }\n\n")
        
        f.write("create_port -direction in TEST_ENABLE\n")
        f.write("create_net TEST_ENABLE\n")
        f.write("connect_net TEST_ENABLE TEST_ENABLE\n\n")
        
        for node, score in selected_nodes:
            f.write("# Node: {} (Weighted Score: {:.2f})\n".format(node, score))
            
            if "reg" in node or "last_" in node or "DFF" in node:
                pin_name = "Q"
            else:
                pin_name = "Y"
                
            full_pin_path = "{" + "{}/{}".format(node, pin_name) + "}"
            clean_name = node.replace("\\", "").replace("[", "_").replace("]", "_")
            xor_inst_name = "TPI_XOR_{}".format(clean_name)
            new_net_name  = "n_tpi_{}".format(clean_name)

            f.write("set target_net [get_nets -of_objects [get_pins {}]]\n".format(full_pin_path))
            f.write("create_cell {{{}}} $lib_cell_ref\n".format(xor_inst_name))
            f.write("disconnect_net $target_net {}\n".format(full_pin_path))
            f.write("connect_net $target_net {}/Y\n".format(xor_inst_name))
            f.write("create_net {}\n".format(new_net_name))
            f.write("connect_net {} {}\n".format(new_net_name, full_pin_path))
            f.write("connect_net {} {}/A1\n".format(new_net_name, xor_inst_name))
            f.write("connect_net TEST_ENABLE {}/A2\n".format(xor_inst_name))
            f.write("\n")

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    circuit = CircuitGraph()
    circuit.parse_verilog(NETLIST_FILE)
    victims = parse_tetramax_failures(REPORT_FILE)
    if victims:
        top_nodes = run_weighted_analysis(circuit, victims)
        generate_tcl_script(top_nodes, circuit)
    else:
        print "Error: No victims found."
