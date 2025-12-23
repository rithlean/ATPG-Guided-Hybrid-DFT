import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "test_scan_b10_tpi.v"
FAULT_REPORT     = "stage2_failures.rpt"
OUTPUT_TCL       = "insert_atomic_fix.tcl"
TEST_PORT_NAME   = "TEST_ENABLE"  # Reusing your existing port

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

        instance_pattern = re.compile(r'([a-zA-Z0-9_]+)\s+([\\a-zA-Z0-9_\[\]]+)\s*\((.*?)\);', re.DOTALL)
        
        for cell_type, inst_name, pin_block in instance_pattern.findall(content):
            pin_pattern = re.compile(r'\.([a-zA-Z0-9_]+)\s*\(\s*([a-zA-Z0-9_\[\]\\]+)\s*\)')
            pin_matches = pin_pattern.findall(pin_block)
            
            pins_map = {}
            for port, net in pin_matches:
                pins_map[port] = net
            
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
                        location = parts[2]
                        if "/" in location:
                            inst, pin = location.split('/')
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
            if ("\\" + inst) in analyzer.gates:
                inst = "\\" + inst
            else:
                continue
        
        gate = analyzer.gates[inst]
        g_type = gate['type'].upper()
        
        # Determine Logic
        forcing_action = None
        if "AND" in g_type or "NAND" in g_type:
            forcing_action = "FORCE_1" # Need 1 to unblock
        elif "OR" in g_type or "NOR" in g_type or "XOR" in g_type:
            forcing_action = "FORCE_0" # Need 0 to unblock
            
        if not forcing_action: continue

        side_nets = []
        side_pins = []
        
        for pin_name, net_name in gate['pins'].items():
            if pin_name != victim_pin and pin_name not in ['Y', 'Z', 'Q', 'QN']:
                side_nets.append(net_name)
                side_pins.append(pin_name)

        if not side_nets: continue
        
        blocker_net = side_nets[0]
        blocker_pin = side_pins[0]

        duplicate = False
        for fix in fixes:
            if fix['gate'] == inst and fix['gate_pin'] == blocker_pin:
                duplicate = True
                break
        
        if not duplicate:
            # print "    -> MATCH: {}/{} blocked by '{}'".format(inst, victim_pin, blocker_net)
            fixes.append({
                'gate': inst,
                'gate_pin': blocker_pin,
                'side_net': blocker_net,
                'action': forcing_action,
                'victim': f['inst'] + "/" + f['pin']
            })

    return fixes

# ==========================================
# PART 3: GENERATE TCL (INTEGRATED)
# ==========================================
def generate_tcl(fixes, filename):
    print "[*] Generating Atomic Fix TCL: {}...".format(filename)
    
    # Check if we need the inverter (Do we have any FORCE_0 cases?)
    need_inverter = any(f['action'] == "FORCE_0" for f in fixes)
    
    with open(filename, 'w') as f:
        f.write("# Phase 2: Atomic Fix using Existing TEST_ENABLE\n")
        
        f.write("set LIB_OR  [get_lib_cells */OR2*]\n")
        f.write("set LIB_AND [get_lib_cells */AND2*]\n")
        f.write("set LIB_INV [get_lib_cells */INV*]\n") # Need Inverter for polarity
        
        # Note: We do NOT create_port because TEST_ENABLE already exists.
        
        # 1. Create Helper Inverter if needed (for Force 0 logic)
        if need_inverter:
            f.write("\n# --- Helper: Invert TEST_ENABLE for Force 0 Logic ---\n")
            f.write("create_cell U_TE_INV [index_collection $LIB_INV 0]\n")
            f.write("create_net n_TEST_ENABLE_bar\n")
            f.write("connect_net {} U_TE_INV/A\n".format(TEST_PORT_NAME))
            f.write("connect_net n_TEST_ENABLE_bar U_TE_INV/Y\n")
            f.write("# ----------------------------------------------------\n\n")

        count = 0
        for fix in fixes:
            count += 1
            gate_name = fix['gate']
            pin_name  = fix['gate_pin']
            side_net  = fix['side_net']
            
            inst_name = "U_ATOMIC_FIX_{}".format(count)
            safe_net  = "n_safe_{}_{}".format(count, side_net.replace("\\","").replace("[","_").replace("]",""))
            
            f.write("# Fix #{}: Unblocking {} ({})\n".format(count, fix['victim'], fix['action']))
            
            if fix['action'] == "FORCE_1":
                # INSERT OR GATE (Passes 1 when TEST_ENABLE is 1)
                f.write("create_cell {} [index_collection $LIB_OR 0]\n".format(inst_name))
                f.write("create_net {}\n".format(safe_net))
                
                f.write("disconnect_net {} {}/{}\n".format(side_net, gate_name, pin_name))
                f.write("connect_net {} {}/{}\n".format(safe_net, gate_name, pin_name))
                
                f.write("connect_net {} {}/A1\n".format(side_net, inst_name))
                f.write("connect_net {} {}/A2\n".format(TEST_PORT_NAME, inst_name)) 
                f.write("connect_net {} {}/Y\n\n".format(safe_net, inst_name))

            elif fix['action'] == "FORCE_0":
                # INSERT AND GATE (Passes 0 when n_TEST_ENABLE_bar is 0... Wait!)
                # Logic Check: We want Output=0 when Test=1.
                # AND Gate: A1=Side, A2=Control.
                # If Control=0 -> Output=0. 
                # So we connect A2 to n_TEST_ENABLE_bar (which is 0 when Test=1).
                
                f.write("create_cell {} [index_collection $LIB_AND 0]\n".format(inst_name))
                f.write("create_net {}\n".format(safe_net))
                
                f.write("disconnect_net {} {}/{}\n".format(side_net, gate_name, pin_name))
                f.write("connect_net {} {}/{}\n".format(safe_net, gate_name, pin_name))
                
                f.write("connect_net {} {}/A1\n".format(side_net, inst_name))
                f.write("connect_net n_TEST_ENABLE_bar {}/A2\n".format(inst_name)) 
                f.write("connect_net {} {}/Y\n\n".format(safe_net, inst_name))

if __name__ == "__main__":
    analyzer = CircuitAnalyzer()
    analyzer.parse_verilog(NETLIST_FILE)
    analyzer.parse_failures(FAULT_REPORT)
    fixes = find_traps(analyzer)
    if fixes: generate_tcl(fixes, OUTPUT_TCL)
    else: print "No atomic candidates found."
