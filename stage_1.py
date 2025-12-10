import re
import sys
from collections import defaultdict

# ==========================================
# CONFIGURATION SECTION
# ==========================================
# Update these filenames to match your actual files
NETLIST_FILE = "b04_syn.v"          # Your Synthesized Netlist
REPORT_FILE  = "stage1_failures.rpt" # The TetraMAX Report
OUTPUT_TCL   = "insert_tpi_logic.tcl" # The script DC will run
TOP_K_NODES  = 5                     # How many Test Points to insert
TRACE_DEPTH  = 3                     # How many gates backward to trace (Cone Depth)

# ==========================================
# PART 1: NETLIST PARSER (Build the Graph)
# ==========================================
class CircuitGraph:
    def __init__(self):
        # Dictionary: Output_Net -> [Input_Nets]
        # This tells us: "To control this net, you need these input nets"
        self.drivers = defaultdict(list)
        # Dictionary: Instance_Name -> Output_Net
        self.instance_to_output = {}
        # Dictionary: Net -> Instance_Name (Who drives this net?)
        self.net_driver_inst = {}

    def parse_verilog(self, filename):
        print(f"[*] Parsing Netlist: {filename}...")
        with open(filename, 'r') as f:
            content = f.read()

        # Regex to find standard gate instances
        # Format: CellName InstanceName ( .Pin(Net), ... );
        # We assume standard synthesized format
        instance_pattern = re.compile(r'(\w+)\s+(\w+)\s*\((.*?)\);', re.DOTALL)
        
        matches = instance_pattern.findall(content)
        for cell_type, inst_name, pins in matches:
            # Parse Pins inside the parenthesis
            pin_pattern = re.compile(r'\.(\w+)\s*\(\s*(\w+)\s*\)')
            pin_matches = pin_pattern.findall(pins)
            
            inputs = []
            output = None
            
            for pin_name, net_name in pin_matches:
                # Heuristic: Usually 'Y', 'Z', 'Q' are outputs. 'A', 'B', 'D', 'CK' are inputs.
                # Adjust this list based on your SAED32 library!
                if pin_name in ['Y', 'Z', 'Q', 'QN']:
                    output = net_name
                    self.instance_to_output[inst_name] = net_name
                    self.net_driver_inst[net_name] = inst_name
                elif pin_name in ['A', 'B', 'C', 'D', 'SI', 'SE', 'CLK', 'RST']:
                    inputs.append(net_name)
            
            if output:
                self.drivers[output] = inputs

        print(f"    - Parsed {len(matches)} instances.")
        print(f"    - Built connectivity graph with {len(self.drivers)} nodes.")

    def get_fanin_cone(self, start_inst, depth):
        """
        Performs Recursive Backward Trace (Cone Analysis).
        Returns a set of all upstream Instance Names.
        """
        cone_nodes = set()
        
        # Find the net driven by this instance
        start_net = self.instance_to_output.get(start_inst)
        if not start_net:
            return cone_nodes

        def trace(current_net, current_depth):
            if current_depth == 0:
                return
            
            # Find inputs that drive this net
            driver_nets = self.drivers.get(current_net, [])
            
            for d_net in driver_nets:
                # Find the instance driving this input net
                driver_inst = self.net_driver_inst.get(d_net)
                if driver_inst:
                    cone_nodes.add(driver_inst)
                    trace(d_net, current_depth - 1)

        trace(start_net, depth)
        return cone_nodes

# ==========================================
# PART 2: FAILURE PARSER (Identify Victims)
# ==========================================
def parse_tetramax_failures(filename):
    print(f"[*] Parsing Failure Report: {filename}...")
    victims = []
    
    with open(filename, 'r') as f:
        for line in f:
            # Look for lines like: "stuck_at_0   ND   /U123/Z"
            if "ND" in line or "AU" in line:
                # Regex to extract instance name (e.g., U123)
                # It looks for patterns like /U123/ or .U123.
                match = re.search(r'[/.](U\w+)[/.]', line)
                if match:
                    victims.append(match.group(1))
    
    # Remove duplicates
    victims = list(set(victims))
    print(f"    - Found {len(victims)} unique RPR victim nodes.")
    return victims

# ==========================================
# PART 3: THE ALGORITHM (Intersection)
# ==========================================
def run_intersection_heuristic(circuit, victims):
    print("[*] Running Structural Cone Analysis & Intersection Scoring...")
    
    # Dictionary to store Fault Impact Score (S_impact)
    # Key = Node Name, Value = Score (Count of faults passing through)
    node_scores = defaultdict(int)
    
    for i, victim in enumerate(victims):
        # 1. Backward Trace: Get the Fan-In Cone for this fault
        cone = circuit.get_fanin_cone(victim, TRACE_DEPTH)
        
        # 2. Intersection Scoring:
        # Increment score for every node found in this cone
        for node in cone:
            node_scores[node] += 1
            
        # Add the victim itself (as it obviously controls itself)
        node_scores[victim] += 1

    # 3. Ranking: Sort by Score Descending
    sorted_nodes = sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
    
    return sorted_nodes[:TOP_K_NODES]

# ==========================================
# PART 4: TCL GENERATION
# ==========================================
def generate_tcl_script(selected_nodes):
    print(f"[*] Generating TCL Script: {OUTPUT_TCL}...")
    
    with open(OUTPUT_TCL, 'w') as f:
        f.write("# ========================================================\n")
        f.write("# Stage 1: Inversion-Based TPI Insertion Script\n")
        f.write("# Method: ATPG-Guided Cone Intersection Analysis\n")
        f.write("# ========================================================\n\n")
        
        f.write("create_port -direction in TEST_ENABLE\n\n")
        
        for node, score in selected_nodes:
            f.write(f"# Target Node: {node} | Fault Impact Score: {score}\n")
            f.write(f"# Logic: Inserting XOR at output of {node}\n")
            
            # Design Compiler ECO Commands
            # 1. We insert a buffer/XOR wrapper on the output pin
            # NOTE: Verify your library cell names! Assuming 'XOR2X1' exists.
            
            cell_name = f"TPI_XOR_{node}"
            f.write(f"insert_buffer {node}/Y {cell_name} -lib_cell XOR2X1\n")
            f.write(f"connect_net TEST_ENABLE {cell_name}/B\n")
            f.write("\n")
            
    print("[*] Done! Ready for Design Compiler.")

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    # 1. Load Circuit Graph
    circuit = CircuitGraph()
    circuit.parse_verilog(NETLIST_FILE)
    
    # 2. Get Victims
    victims = parse_tetramax_failures(REPORT_FILE)
    
    if not victims:
        print("Error: No failures found in report. Check TetraMAX output.")
        sys.exit(1)
        
    # 3. Run Methodology
    top_nodes = run_intersection_heuristic(circuit, victims)
    
    print("\n[RESULT] Top Selected Nodes for Inversion:")
    for node, score in top_nodes:
        print(f"  - {node}: Covers {score} RPR faults")
        
    # 4. Generate Output
    generate_tcl_script(top_nodes)