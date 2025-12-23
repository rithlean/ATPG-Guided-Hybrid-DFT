import re
import sys

# ==========================================
# CONFIGURATION
# ==========================================
NETLIST_FILE     = "test_scan_b10_tpi.v"
FAULT_REPORT     = "stage2_failures.rpt"
OUTPUT_TCL       = "insert_atomic_fix.tcl"

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
        blocker_pin = side_pins[0] # Crucial: We need to know WHICH pin to disconnect

        # 3. Create Fix Entry
        # Avoid duplicate fixes on the same gate/pin
        duplicate = False
        for fix in fixes:
            if fix['gate'] == inst and fix['gate_pin'] == blocker_pin:
                duplicate = True
                break
        
        if not duplicate:
            # Shorten message for cleaner printing
            print "    -> MATCH: {}/{} blocked by '{}'".format(inst, victim_pin, blocker_net)
            fixes.append({
                'gate': inst,
                'gate_pin': blocker_pin, # e.g., "A2
