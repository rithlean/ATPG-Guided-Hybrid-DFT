import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "test_scan_b10_tpi.v" # Your netlist
FAILURE_RPT      = "stage2_failures.rpt" # To know which gates we fixed
OUTPUT_TCL       = "insert_xor_trees.tcl"
OBS_PORT_NAME    = "TEST_OBSERVE"        # New output pin to see the faults

# ==========================================
# PART 1: ANALYZE THE FIXES
# ==========================================
# We need to re-run the logic to find WHERE we put the fixes
# so we know what to observe.
class CircuitAnalyzer:
    def __init__(self):
        self.gates = {} 
        self.faults = [] 

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist..."
        with open(filename, 'r') as f:
            content = f.read()
        # Find gates to map names to outputs
        instance_pattern = re.compile(r'([a-zA-Z0-9_]+)\s+([\\a-zA-Z0-9_\[\]]+)\s*\((.*?)\);', re.DOTALL)
        for cell_type, inst_name, pin_block in instance_pattern.findall(content):
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
                    if len(parts) >= 3:
                        location = parts[2]
                        if "/" in location:
                            inst, pin = location.split('/')
                            # We want to observe the OUTPUT of this instance
                            self.faults.append(inst)
        except IOError:
            print "Error reading report."

# ==========================================
# PART 2: GENERATE XOR TREE TCL
# ==========================================
def generate_xor_tcl(analyzer, filename):
    print "[*] Generating XOR Observation Logic: {}...".format(filename)
    
    # 1. Identify Observation Points
    # The "Victim" gates from Stage 2 are now transparent.
    # We must observe their Output Pins (Y, Q, Z, etc.)
    obs_nets = []
    seen_gates = set()
    
    for inst in analyzer.faults:
        if inst in seen_gates: continue
        if inst not in analyzer.gates: 
             if ("\\" + inst) in analyzer.gates: inst = "\\" + inst
             else: continue
        
        seen_gates.add(inst)
        gate_info = analyzer.gates[inst]
        
        # Find the output net
        # Heuristic: Look for pins named Y, Q, Z, or SO
        out_net = None
        for p in ['Y', 'Q', 'Z', 'SO']:
            if p in gate_info['pins']:
                out_net = gate_info['pins'][p]
                break
        
        if out_net:
            obs_nets.append(out_net)

    # Remove duplicates
    obs_nets = sorted(list(set(obs_nets)))
    print "    - Found {} points to observe.".format(len(obs_nets))

    with open(filename, 'w') as f:
        f.write("# Phase 3: Observation XOR Tree\n")
        f.write("set LIB_XOR [get_lib_cells */XOR2*]\n")
        f.write("create_port -direction out {}\n".format(OBS_PORT_NAME))
        
        # Build Tree
        current_layer = obs_nets
        layer_num = 0
        gate_num = 0
        
        while len(current_layer) > 1:
            layer_num += 1
            next_layer = []
            f.write("\n# Layer {}\n".format(layer_num))
            
            for i in range(0, len(current_layer), 2):
                if i+1 < len(current_layer):
                    gate_num += 1
                    inst = "U_OBS_XOR_{}_{}".format(layer_num, gate_num)
                    net_out = "n_obs_{}_{}".format(layer_num, gate_num)
                    
                    net_a = current_layer[i]
                    net_b = current_layer[i+1]
                    
                    f.write("create_cell {} [index_collection $LIB_XOR 0]\n".format(inst))
                    f.write("create_net {}\n".format(net_out))
                    f.write("connect_net {} {}/A\n".format(net_a, inst))
                    f.write("connect_net {} {}/B\n".format(net_b, inst))
                    f.write("connect_net {} {}/Y\n".format(net_out, inst))
                    
                    next_layer.append(net_out)
                else:
                    # Pass through odd net
                    next_layer.append(current_layer[i])
            current_layer = next_layer
            
        # Connect final net to port
        if current_layer:
            f.write("\n# Final Connect\n")
            f.write("connect_net {} {}\n".format(current_layer[0], OBS_PORT_NAME))

if __name__ == "__main__":
    analyzer = CircuitAnalyzer()
    analyzer.parse_verilog(NETLIST_FILE)
    analyzer.parse_failures(FAILURE_RPT)
    generate_xor_tcl(analyzer, OUTPUT_TCL)
