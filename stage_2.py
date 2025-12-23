import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "test_scan_b10_tpi.v"
FAULT_REPORT     = "stage2_failures.rpt"
OUTPUT_TCL       = "insert_atomic_fix.tcl"

# Keywords to identify side-inputs (Control Signals)
# Added 'n' to catch internal nets like 'n218' if they block critical logic
SIDE_INPUT_KEYWORDS = ["reset", "rst", "clear", "en", "valid", "test", "scan", "start", "key", "button", "n"]

# ==========================================
# PART 1: PARSERS
# ==========================================
class CircuitAnalyzer:
    def __init__(self):
        self.gates = {} 
        self.faults = [] 

    def parse_verilog(self, filename):
        print "[*] Parsing Netlist: {}...".format(filename)
        with open(filename, 'r') as f:
            content = f.read()

        # Regex: Type Name ( .Pin(Net) ... )
        instance_pattern = re.compile(r'([a-zA-Z0-9_]+)\s+([\\a-zA-Z0-9_\[\]]+)\s*\((.*?)\);', re.DOTALL)
        
        for cell_type, inst_name, pin_block in instance_pattern.findall(content):
            # Parse Pins
            pin_pattern = re.compile(r'\.([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_\[\]\\]+)\s*\)')
            pin_matches = pin_pattern.findall(pin_block)
            
            pins_map = {}
            for port, net in pin_matches:
                pins_map[port] = net
            
            # Clean up instance name (remove leading backslash if present for dict key)
            clean_name = inst_name.strip()
            self.gates[clean_name] = {
                'type': cell_type,
                'pins': pins_map
            }
        print "    - Indexed {} gates.".format(len(self.gates))

    def parse_failures(self, filename):
        print "[*] Parsing Fault List: {}...".format(filename)
        try:
            with open(filename, 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        ftype = parts[0]
                        location = parts[2] # e.g., "U140/A1"
                        
                        if "/" in location:
                            inst, pin = location.split('/')
                            # Handle escaped names in report if they differ
                            self.faults.append({'inst': inst, 'pin': pin, 'type': ftype})
        except IOError:
            print "Error: Could not read fault report."

# ==========================================
# PART 2: TRAP LOGIC
# ==========================================
def find_traps(analyzer):
    print "[*] Correlating Faults with Blocking Gates..."
    fixes = []
    
    for f in analyzer.faults:
        inst = f['inst']
        victim_pin = f['pin']
        
        if inst not in analyzer.gates: 
            # Try adding backslash if missing
            if ("\\" + inst) in analyzer.gates:
                inst = "\\" + inst
            else:
                continue
        
        gate = analyzer.gates[inst]
        g_type = gate['type'].upper()
        
        # 1. Logic Type Check
        forcing_action = None
        if "AND" in g_type or "NAND" in g_type:
            forcing_action = "FORCE_1"
        elif "OR" in g_type or "NOR" in g_type:
            forcing_action = "FORCE_0"
            
        if not forcing_action: continue

        # 2. Find Side Inputs (The Blockers)
        side_nets = []
        side_pins = []
        
        for pin_name, net_name in gate['pins'].items():
            # Skip the victim pin and output pins
            if pin_name != victim_pin and pin_name not in ['Y', 'Z', 'Q', 'QN']:
                side_nets.append(net_name)
                side_pins.append(pin_name)

        if not side_nets: continue
        
        # Take the first blocker found (Simplification for Atomic Fix)
        blocker_net = side_nets[0]
        blocker_pin = side_pins[0] # Crucial: We need to know WHICH pin to disconnect (A1? A2?)

        # 3. Create Fix Entry
        # Avoid duplicate fixes on the same gate/pin
        duplicate = False
        for fix in fixes:
            if fix['gate'] == inst and fix['gate_pin'] == blocker_pin:
                duplicate = True
                break
        
        if not duplicate:
            print "    -> MATCH: Fault on {}/{} blocked by '{}' (Pin {})".format(inst, victim_pin, blocker_net, blocker_pin)
            fixes.append({
                'gate': inst,
                'gate_pin': blocker_pin, # e.g., "A2"
                'side_net': blocker_net, # e.g., "n218"
                'action': forcing_action,
                'victim': f['inst'] + "/" + f['pin']
            })

    return fixes

# ==========================================
# PART 3: GENERATE TCL (Customized for LVT)
# ==========================================
def generate_tcl(fixes, filename):
    print "[*] Generating Atomic Fix TCL: {}...".format(filename)
    with open(filename, 'w') as f:
        f.write("# Phase 2: Fault-Aware Atomic Sensitization (Custom LVT)\n")
        
        # UPDATED LIBRARY SEARCH FOR LVT CELLS
        f.write("set LIB_OR  [get_lib_cells */OR2*] ;# Generic match to find OR2X1_LVT\n")
        f.write("set LIB_AND [get_lib_cells */AND2*]\n")
        f.write("if {[sizeof_collection $LIB_OR] == 0} { echo \"WARNING: No OR2 cell found!\" }\n")
        f.write("create_port -direction in TEST_MODE\n\n")

        count = 0
        for fix in fixes:
            count += 1
            gate_name = fix['gate']
            pin_name  = fix['gate_pin'] # e.g., "A1" or "A2"
            side_net  = fix['side_net']
            
            inst_name = "U_ATOMIC_FIX_{}".format(count)
            safe_net  = "n_safe_{}_{}".format(count, side_net.replace("\\","").replace("[","_").replace("]",""))
            
            f.write("# Fix #{}: Unblocking {} (Blocked by {} at pin {})\n".format(count, fix['victim'], side_net, pin_name))
            
            if fix['action'] == "FORCE_1":
                # INSERT OR GATE
                f.write("create_cell {} [index_collection $LIB_OR 0]\n".format(inst_name))
                f.write("create_net {}\n".format(safe_net))
                
                # Disconnect the specific pin (A1 or A2)
                f.write("disconnect_net {} {}/{}\n".format(side_net, gate_name, pin_name))
                f.write("connect_net {} {}/{}\n".format(safe_net, gate_name, pin_name))
                
                # Connect Fix Logic (Assume library cells use A1/A2 or A/B)
                # We use -no_warn because we don't know if your OR2 uses A/B or A1/A2.
                # Standard Synopsys usually uses A1/A2 for logic.
                f.write("connect_net {} {}/A1\n".format(side_net, inst_name))
                f.write("connect_net TEST_MODE {}
