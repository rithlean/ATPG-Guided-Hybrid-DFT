import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "b10.v"
FAULT_REPORT     = "failures.rpt"  # The list of undetected faults (from Tetramax)
OUTPUT_TCL       = "insert_atomic_fix.tcl"

# Keywords to identify side-inputs (Control Signals)
SIDE_INPUT_KEYWORDS = ["reset", "rst", "clear", "clr", "enable", "en", "valid", "test", "scan"]

# ==========================================
# PART 1: PARSERS
# ==========================================
class CircuitAnalyzer:
    def __init__(self):
        self.gates = {}     # Map: {output_net: {name, type, inputs, pins}}
        self.victims = set() # Set of nets that have faults

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            content = f.read()

        # Regex to capture instances: Type Name ( .Pin(Net) ... )
        instance_pattern = re.compile(r'([a-zA-Z0-9_]+)\s+([a-zA-Z0-9_]+)\s*\((.*?)\);', re.DOTALL)
        
        count = 0
        for cell_type, inst_name, pin_block in instance_pattern.findall(content):
            # Parse Pins
            pin_pattern = re.compile(r'\.([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_\[\]]+)\s*\)')
            pin_matches = pin_pattern.findall(pin_block)
            
            inputs = []
            output_net = None
            pins_map = {}

            for port, net in pin_matches:
                pins_map[port] = net
                if port in ['Y', 'Z', 'Q', 'QN', 'SO']: # Standard Output Pin Names
                    output_net = net
                else:
                    inputs.append(net)

            if output_net:
                self.gates[output_net] = {
                    'name': inst_name,
                    'type': cell_type,
                    'inputs': inputs,
                    'pins': pins_map
                }
                count += 1
        print "    - Indexed {} gates by output net.".format(count)

    def parse_failures(self, filename):
        print "[*] Parsing Fault List: {}...".format(filename)
        try:
            with open(filename, 'r') as f:
                for line in f:
                    # Look for lines like: "stuck_at_0   U_AND_15/A" or just the net name
                    parts = line.split()
                    if len(parts) > 0:
                        path = parts[-1] # Usually the last item is the pin/net
                        
                        # Clean up: Extract the Net Name or Instance Name
                        # If report gives "U15/Y", we want to know the net driven by U15
                        if "/" in path:
                            inst_name = path.split('/')[0]
                            # Find the net driven by this instance (Reverse lookup)
                            # (For simplicity in this script, we assume strict net names or verify manually)
                            # Better approach: Just store the instance name as a 'suspect'
                            self.victims.add(inst_name)
                        else:
                            self.victims.add(path)
                            
            print "    - Found {} fault locations.".format(len(self.victims))
        except IOError:
            print "Error: Could not read fault report."

# ==========================================
# PART 2: THE "TRAP HUNTER" ALGORITHM
# ==========================================
def find_and_fix_traps(analyzer):
    print "[*] correlating Faults with Blocking Gates..."
    fixes = []
    
    # Iterate through all gates to see if they are "Traps" for our Victims
    for out_net, gate in analyzer.gates.items():
        
        # CHECK 1: Is this gate consuming a 'Victim' signal?
        # We look at the inputs of this gate.
        victim_input = None
        for inp in gate['inputs']:
            # Check if this input comes from a known faulty instance/net
            # (Simplified matching: if input net name contains victim name)
            for v in analyzer.victims:
                if v in inp: 
                    victim_input = inp
                    break
        
        if not victim_input: continue # This gate isn't carrying a fault we care about.

        # CHECK 2: Is it a Blocking Gate (AND/OR)?
        g_type = gate['type'].upper()
        forcing_action = None
        
        if "AND" in g_type or "NAND" in g_type:
            forcing_action = "FORCE_1"
        elif "OR" in g_type or "NOR" in g_type:
            forcing_action = "FORCE_0"
            
        if not forcing_action: continue # Pass-through logic (buffers, XORS) don't block.

        # CHECK 3: Does it have a "Side Input" that looks like a Control Signal?
        side_input = None
        for inp in gate['inputs']:
            if inp == victim_input: continue # Skip the fault itself
            
            # Heuristic: Is this a Reset/Enable line?
            if any(k in inp.lower() for k in SIDE_INPUT_KEYWORDS):
                side_input = inp
                break
        
        if side_input:
            print "    -> MATCH: Fault on '{}' blocked by Gate '{}' via Control '{}'".format(
                victim_input, gate['name'], side_input
            )
            
            fixes.append({
                'gate': gate['name'],
                'side_net': side_input,
                'action': forcing_action,
                'victim': victim_input
            })

    return fixes

# ==========================================
# PART 3: GENERATE TCL
# ==========================================
def generate_tcl(fixes, filename):
    print "[*] Generating Atomic Fix TCL: {}...".format(filename)
    with open(filename, 'w') as f:
        f.write("# Phase 2: Fault-Aware Atomic Sensitization\n")
        f.write("set LIB_OR  [get_lib_cells */OR2X1]\n")
        f.write("set LIB_AND [get_lib_cells */AND2X1]\n")
        f.write("create_port -direction in TEST_MODE\n\n")

        count = 0
        for fix in fixes:
            count += 1
            inst_name = "U_ATOMIC_FIX_{}".format(count)
            safe_net  = "n_safe_{}_{}".format(count, fix['side_net'])
            
            f.write("# Fix #{}: Unblocking fault on {}\n".format(count, fix['victim']))
            
            if fix['action'] == "FORCE_1":
                # INSERT OR GATE
                f.write("create_cell {} $LIB_OR\n".format(inst_name))
                f.write("create_net {}\n".format(safe_net))
                f.write("disconnect_net {} [get_pins {}/A]\n".format(fix['side_net'], fix['gate'])) # Assume pin A is side input (Logic needs refinement here for pin mapping)
                 # Note: In real TCL, we filter pins by net name like in the previous script
                f.write("connect_net {} [get_pins {}/A]\n".format(safe_net, fix['gate']))
                
                f.write("connect_net {} {}/A\n".format(fix['side_net'], inst_name))
                f.write("connect_net TEST_MODE {}/B\n".format(inst_name))
                f.write("connect_net {} {}/Y\n\n".format(safe_net, inst_name))

# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    analyzer = CircuitAnalyzer()
    analyzer.parse_verilog(NETLIST_FILE)
    analyzer.parse_failures(FAULT_REPORT)
    
    fixes = find_and_fix_traps(analyzer)
    
    if fixes:
        generate_tcl(fixes, OUTPUT_TCL)
    else:
        print "No blocked faults found matching criteria."
