import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "test_scan_b10_tpi.v"  # Your netlist name
FAULT_REPORT     = "stage2_failures.rpt"  # Your report name
OUTPUT_TCL       = "insert_atomic_fix.tcl"

# Relaxed Heuristic: If we can't identify a "Control" name, 
# we still flag it if it blocks a valid fault.
# (You can restrict this list to ["reset", "rst"] if you want fewer fixes)
SIDE_INPUT_KEYWORDS = ["reset", "rst", "clear", "en", "valid", "test", "scan", "cntrl"]

# ==========================================
# PART 1: ROBUST PARSING
# ==========================================
class CircuitAnalyzer:
    def __init__(self):
        # Map: { "U139": {'type': 'AND2', 'pins': {'A1': 'n1', 'A2': 'n2', 'Y': 'n3'}} }
        self.gates = {} 
        self.faults = [] # List of {'inst': 'U139', 'pin': 'A2', 'type': 'sa1'}

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            content = f.read()

        # Regex: Type Name ( .Pin(Net) ... )
        instance_pattern = re.compile(r'([a-zA-Z0-9_]+)\s+([a-zA-Z0-9_]+)\s*\((.*?)\);', re.DOTALL)
        
        for cell_type, inst_name, pin_block in instance_pattern.findall(content):
            pin_pattern = re.compile(r'\.([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_\[\]\\]+)\s*\)')
            pin_matches = pin_pattern.findall(pin_block)
            
            pins_map = {}
            for port, net in pin_matches:
                pins_map[port] = net
            
            self.gates[inst_name] = {
                'type': cell_type,
                'pins': pins_map
            }
        print "    - Indexed {} gates.".format(len(self.gates))

    def parse_failures(self, filename):
        print "[*] Parsing Fault List: {}...".format(filename)
        try:
            with open(filename, 'r') as f:
                for line in f:
                    # Format: sa1  NO  U139/A2
                    parts = line.split()
                    if len(parts) >= 3:
                        ftype = parts[0] # "sa1" or "sa0"
                        status = parts[1]
                        location = parts[2] # "U139/A2" or "r_button"
                        
                        if "/" in location:
                            inst, pin = location.split('/')
                            self.faults.append({'inst': inst, 'pin': pin, 'type': ftype})
                        else:
                            # It's a net fault (e.g. "r_button"). Skip for Atomic (Phase 2 targets gates)
                            pass
                            
            print "    - Found {} pin-level faults.".format(len(self.faults))
        except IOError:
            print "Error: Could not read fault report."

# ==========================================
# PART 2: TRAP LOGIC (FIXED)
# ==========================================
def find_traps(analyzer):
    print "[*] Correlating Faults with Blocking Gates..."
    fixes = []
    
    for f in analyzer.faults:
        inst = f['inst']
        victim_pin = f['pin']
        
        if inst not in analyzer.gates: continue
        
        gate = analyzer.gates[inst]
        g_type = gate['type'].upper()
        
        # 1. CHECK IF GATE IS A BLOCKER (AND/OR/NAND/NOR)
        forcing_action = None
        if "AND" in g_type or "NAND" in g_type:
            # AND is blocked if side input is 0. 
            forcing_action = "FORCE_1" 
        elif "OR" in g_type or "NOR" in g_type:
            # OR is blocked if side input is 1.
            forcing_action = "FORCE_0"
            
        if not forcing_action: continue # Skip buffers, XORs, DFFs

        # 2. FIND THE SIDE INPUT (The Blocker)
        # The victim pin is stuck. The OTHER pins are the side inputs.
        side_inputs = []
        for pin_name, net_name in gate['pins'].items():
            if pin_name != victim_pin and pin_name not in ['Y', 'Z', 'Q']:
                side_inputs.append(net_name)

        if not side_inputs: continue

        # 3. APPLY FILTER (Optional)
        # We only fix it if the side input looks suspicious (control signal) OR if you want to be aggressive.
        # For now, let's grab the first side input we find.
        blocker_net = side_inputs[0]
        
        # Check against keywords (optional safety filter)
        is_suspicious = any(k in blocker_net.lower() for k in SIDE_INPUT_KEYWORDS)
        
        # RULE: If it matches a keyword, OR if it's a high-fanout net (heuristic), we fix it.
        # Let's be aggressive: Fix it if it exists.
        
        # Dedup: Don't fix the same gate/net twice
        if not any(fix['gate'] == inst and fix['side_net'] == blocker_net for fix in fixes):
            print "    -> MATCH: Fault on {}/{} blocked by '{}'".format(inst, victim_pin, blocker_net)
            fixes.append({
                'gate': inst,
                'side_net': blocker_net,
                'action': forcing_action,
                'victim': f['inst'] + "/" + f['pin']
            })

    return fixes

# ==========================================
# PART 3: GENERATE TCL
# ==========================================
def generate_tcl(fixes, filename):
    print "[*] Generating Atomic Fix TCL: {}...".format(filename)
    with open(filename, 'w') as f:
        f.write("# Atomic Sensitization Fixes\n")
        f.write("set LIB_OR  [get_lib_cells */OR2X1]\n")
        f.write("set LIB_AND [get_lib_cells */AND2X1]\n")
        f.write("create_port -direction in TEST_MODE\n\n")

        count = 0
        for fix in fixes:
            count += 1
            gate_name = fix['gate']
            side_net = fix['side_net']
            inst_name = "U_ATOMIC_FIX_{}".format(count)
            safe_net  = "n_safe_{}_{}".format(count, side_net.replace("\\","").replace("[","_").replace("]",""))
            
            f.write("# Fix #{}: Unblocking {} (Blocked by {})\n".format(count, fix['victim'], side_net))
            
            # Identify the PIN on the gate connected to the side_net
            # (In TCL, we search for the pin on this instance connected to this net)
            
            if fix['action'] == "FORCE_1":
                # INSERT OR GATE (Logic: Side_Net OR Test_Mode)
                f.write("create_cell {} $LIB_OR\n".format(inst_name))
                f.write("create_net {}\n".format(safe_net))
                
                # Disconnect old net from specific pin
                f.write("set target_pin [get_pins -of_objects [get_cells {}] -filter \"net_name=={}\"]\n".format(gate_name, side_net))
                f.write("disconnect_net {} $target_pin\n".format(side_net))
                f.write("connect_net {} $target_pin\n".format(safe_net))
                
                # Wire the Fix
                f.write("connect_net {} {}/A\n".format(side_net, inst_name))
                f.write("connect_net TEST_MODE {}/B\n".format(inst_name))
                f.write("connect_net {} {}/Y\n\n".format(safe_net, inst_name))

            elif fix['action'] == "FORCE_0":
                # INSERT AND GATE (Logic: Side_Net AND !Test_Mode)
                # Note: Assuming direct connection for demo. In real silicon, ensure !Test_Mode exists.
                f.write("create_cell {} $LIB_AND\n".format(inst_name))
                f.write("create_net {}\n".format(safe_net))
                
                f.write("set target_pin [get_pins -of_objects [get_cells {}] -filter \"net_name=={}\"]\n".format(gate_name, side_net))
                f.write("disconnect_net {} $target_pin\n".format(side_net))
                f.write("connect_net {} $target_pin\n".format(safe_net))
                
                f.write("connect_net {} {}/A\n".format(side_net, inst_name))
                f.write("connect_net TEST_MODE {}/B\n".format(inst_name)) # WARNING: Needs Inverter in real life
                f.write("connect_net {} {}/Y\n\n".format(safe_net, inst_name))

if __name__ == "__main__":
    analyzer = CircuitAnalyzer()
    analyzer.parse_verilog(NETLIST_FILE)
    analyzer.parse_failures(FAULT_REPORT)
    fixes = find_traps(analyzer)
    if fixes: generate_tcl(fixes, OUTPUT_TCL)
    else: print "No atomic candidates found."
