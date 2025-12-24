import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "test_scan_b10_tpi.v" 
FAILURE_RPT      = "stage2_failures.rpt" # Ensure this file exists from your previous analysis!
OUTPUT_TCL       = "insert_xor_trees.tcl"
OBS_PORT_NAME    = "TEST_OBSERVE"

# --- LIBRARY PIN NAMES (UPDATED FOR YOUR NETLIST) ---
PIN_IN1 = "A1"   
PIN_IN2 = "A2"
PIN_OUT = "Y"

# ==========================================
# PART 1: ANALYZE THE FIXES
# ==========================================
class CircuitAnalyzer:
    def __init__(self):
        self.gates = {} 
        self.faults = [] 

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist..."
        with open(filename, 'r') as f:
            content = f.read()
        
        # Regex to capture instance name and pin block
        # Handles escaped names like \stato_reg[0]
        instance_pattern = re.compile(r'([a-zA-Z0-9_]+)\s+([\\a-zA-Z0-9_\[\]]+)\s*\((.*?)\);', re.DOTALL)
        
        for cell_type, inst_name, pin_block in instance_pattern.findall(content):
            # Regex to find .PIN(NET)
            pin_pattern = re.compile(r'\.([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_\[\]\\]+)\s*\)')
            pin_matches = pin_pattern.findall(pin_block)
            pins = {p: n for p, n in pin_matches}
            
            clean_name = inst_name.strip()
            self.gates[clean_name] = {'pins': pins}

    def parse_failures(self, filename):
        print "[*] Parsing Failures..."
        try:
            with open(filename, 'r') as f:
                for line in f:
                    parts = line.split()
                    # Expecting format: "stuck_0   U123/Y" or similar
                    if len(parts) >= 3:
                        location = parts[2]
                        if "/" in location:
                            inst, pin = location.split('/')
                            self.faults.append(inst)
        except IOError:
            print "Error reading report: {}".format(filename)

# ==========================================
# PART 2: GENERATE XOR TREE TCL
# ==========================================
def generate_xor_tcl(analyzer, filename):
    print "[*] Generating XOR Observation Logic: {}...".format(filename)
    
    obs_nets = []
    seen_gates = set()
    
    # Find the output nets of the failing instances
    for inst in analyzer.faults:
        if inst in seen_gates: continue
        
        # Handle case where report has "U123" but netlist has "\U123 "
        lookup_name = inst
        if lookup_name not in analyzer.gates: 
             if ("\\" + inst) in analyzer.gates: lookup_name = "\\" + inst
             else: continue
        
        seen_gates.add(inst)
        gate_info = analyzer.gates[lookup_name]
        
        # Heuristic to find output pin (Y, Q, Z, etc)
        out_net = None
        for p in ['Y', 'Q', 'QN', 'Z', 'SO', 'out', 'OUT']:
            if p in gate_info['pins']:
                out_net = gate_info['pins'][p]
                # Prefer Q over QN if both exist, but take what we can get
                if p == 'Q': break 
                
        if out_net:
            obs_nets.append(out_net)

    # Sort to ensure deterministic TCL generation
    obs_nets = sorted(list(set(obs_nets)))
    print "    - Found {} points to observe.".format(len(obs_nets))

    if len(obs_nets) == 0:
        print "WARNING: No observation points found. Check your failure report format."
        return

    with open(filename, 'w') as f:
        f.write("# Phase 3: Observation XOR Tree\n")
        # Note: Generic wildcards might pick up XOR3/XOR4, so we specify XOR2*
        f.write("set LIB_XOR [get_lib_cells */XOR2*]\n")
        f.write("create_port -direction out {}\n".format(OBS_PORT_NAME))
        
        current_layer = obs_nets
        layer_num = 0
        gate_num = 0
        
        # Loop until we have compressed everything to 1 wire
        while len(current_layer) > 1:
            layer_num += 1
            next_layer = []
            f.write("\n# --- Layer {} ---\n".format(layer_num))
            
            for i in range(0, len(current_layer), 2):
                if i+1 < len(current_layer):
                    gate_num += 1
                    inst = "U_OBS_XOR_{}_{}".format(layer_num, gate_num)
                    net_out = "n_obs_{}_{}".format(layer_num, gate_num)
                    
                    net_a = current_layer[i]
                    net_b = current_layer[i+1]
                    
                    # Create the XOR gate
                    f.write("create_cell {} [index_collection $LIB_XOR 0]\n".format(inst))
                    f.write("create_net {}\n".format(net_out))
                    
                    # Connect using YOUR library specific pins (A1, A2, Y)
                    f.write("connect_net {} {}/{}\n".format(net_a, inst, PIN_IN1))
                    f.write("connect_net {} {}/{}\n".format(net_b, inst, PIN_IN2))
                    f.write("connect_net {} {}/{}\n".format(net_out, inst, PIN_OUT))
                    
                    next_layer.append(net_out)
                else:
                    # Odd number of signals; pass this one to the next layer
                    next_layer.append(current_layer[i])
            current_layer = next_layer
            
        if current_layer:
            f.write("\n# --- Final Connect ---\n")
            f.write("connect_net {} {}\n".format(current_layer[0], OBS_PORT_NAME))

if __name__ == "__main__":
    analyzer = CircuitAnalyzer()
    analyzer.parse_verilog(NETLIST_FILE)
    analyzer.parse_failures(FAILURE_RPT)
    generate_xor_tcl(analyzer, OUTPUT_TCL)
