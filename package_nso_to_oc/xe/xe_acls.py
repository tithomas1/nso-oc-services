#! /usr/bin/env python3
"""
Translate NSO Device config to MDD OpenConfig

This script will pull a device's configuration from an NSO server, convert the NED structured configuration to
MDD OpenConfig, save the NSO configuration to a file named {device_name}_ned_configuration_network_instances.json,
save the NSO device configuration minus parts replaced by OpenConfig to a file named
{device_name}_ned_configuration_remaining_network_instances.json, and save the MDD OpenConfig configuration to a file
named {nso_device}_openconfig_acls.json.

The script requires the following environment variables:
NSO_HOST - IP address or hostname for the NSO server
NSO_USERNAME
NSO_PASSWORD
NSO_DEVICE - NSO device name for configuration translation
TEST - True or False. True enables sending the OpenConfig to the NSO server after generation
"""

import sys
from importlib.util import find_spec
from ipaddress import IPv4Network
import socket
import re

acls_notes = []
openconfig_acls = {
    "openconfig-acl:acl": {
        "openconfig-acl:acl-sets": {
            "openconfig-acl:acl-set": []
        },
        "openconfig-acl:interfaces": {
            "openconfig-acl:interface": []
        }
    }
}
protocols_oc_to_xe = {
    "icmp": "IP_ICMP",
    "igmp": "IP_IGMP",
    "ipnip": "IP_IN_IP",
    "tcp": "IP_TCP",
    "udp": "IP_UDP",
    "gre": "IP_GRE",
    "ahp": "IP_AUTH",
    "pim": "IP_PIM"
}
# OC has an additional forwarding action, "DROP", which also translates to "deny" in XE.
actions_xe_to_oc = {
    "permit": "ACCEPT",
    "deny": "REJECT",
}
port_operators = ["range", "eq", "lt", "gt", "neq"]
ACL_STD_TYPE = "ACL_IPV4_STANDARD"
ACL_EXT_TYPE = "ACL_IPV4"

def xe_acls(config_before, config_after):
    oc_acl_set = openconfig_acls["openconfig-acl:acl"]["openconfig-acl:acl-sets"]["openconfig-acl:acl-set"]
    oc_acl_interface = openconfig_acls["openconfig-acl:acl"]["openconfig-acl:interfaces"]["openconfig-acl:interface"]
    access_list = config_before.get("tailf-ned-cisco-ios:ip", {}).get("access-list", {})
    access_list_after = config_after.get("tailf-ned-cisco-ios:ip", {}).get("access-list", {})
    interfaces_by_acl = get_interfaces_by_acl(config_before, config_after)
    acl_interfaces = {}

    for std_index, std_acl in enumerate(access_list.get("standard", {}).get("std-named-acl", [])):
        standard_acl = StandardAcl(oc_acl_set, std_acl, access_list_after["standard"]["std-named-acl"][std_index])
        standard_acl.process_acl()
        process_interfaces(ACL_STD_TYPE, std_acl["name"], interfaces_by_acl, acl_interfaces)
    for ext_index, ext_acl in enumerate(access_list.get("extended", {}).get("ext-named-acl", [])):
        extended_acl = ExtendedAcl(oc_acl_set, ext_acl, access_list_after["extended"]["ext-named-acl"][ext_index])
        extended_acl.process_acl()
        process_interfaces(ACL_EXT_TYPE, ext_acl["name"], interfaces_by_acl, acl_interfaces)
    for interface in acl_interfaces.values():
        oc_acl_interface.append(interface)

    process_ntp(config_before, config_after)
    process_line(config_before, config_after)

class BaseAcl:
    def __init__(self, oc_acl_set, xe_acl_set, xe_acl_set_after):
        self._oc_acl_set = oc_acl_set
        self._xe_acl_set = xe_acl_set
        self._xe_acl_set_after = xe_acl_set_after
        self._xe_acl_name = self._xe_acl_set.get("name")

    def process_acl(self):
        acl_set = {
            "openconfig-acl:name": self._xe_acl_set.get("name"),
            "openconfig-acl:type": self._acl_type,
            "openconfig-acl:config": {
                "openconfig-acl:name": self._xe_acl_set.get("name"),
                "openconfig-acl:type": self._acl_type,
                "openconfig-acl:description": self._xe_acl_set.get("name"), # XE doesn't seem to have a description.
            },
            "openconfig-acl:acl-entries": {
                "openconfig-acl:acl-entry": []
            }
        }
        self._oc_acl_set.append(acl_set)
        acl_success = True
        
        for rule_index, access_rule in enumerate(self._xe_acl_set.get(self._rule_list_key, [])):
            rule_success = self.__set_rule_parts(access_rule, acl_set)
            
            if rule_success:
                self._xe_acl_set_after[self._rule_list_key][rule_index] = None
            else:
                acl_success = False

        # We only delete if all entries processed successfully.
        if acl_success:
            del self._xe_acl_set_after["name"]

    def __set_rule_parts(self, access_rule, acl_set):
        rule_parts = access_rule.get("rule", "").split()
        
        if len(rule_parts) < 1:
            return

        success = True
        seq_id = int(rule_parts[0])
        entry = {
            "openconfig-acl:sequence-id": seq_id,
            "openconfig-acl:config": {"openconfig-acl:sequence-id": seq_id},
            "openconfig-acl:actions": {
                "openconfig-acl:config": {"openconfig-acl:forwarding-action": actions_xe_to_oc[rule_parts[1]]}
            }
        }

        try:
            current_index = self.__set_protocol(entry, rule_parts)
            # Source IP
            current_index = self.__set_ip_and_port(rule_parts, current_index, entry, True)
            if self._acl_type == "ACL_IPV4":
                # Destination IP (if exists)
                current_index = self.__set_ip_and_port(rule_parts, current_index, entry, False)
        except Exception as err:
            success = False

        if len(rule_parts) > current_index and rule_parts[current_index] == "log-input":
            entry["openconfig-acl:actions"]["openconfig-acl:config"]["openconfig-acl:log-action"] = "LOG_SYSLOG"
        else:
            entry["openconfig-acl:actions"]["openconfig-acl:config"]["openconfig-acl:log-action"] = "LOG_NONE"

        if success:
            acl_set["openconfig-acl:acl-entries"]["openconfig-acl:acl-entry"].append(entry)

        return success
    
    def __add_acl_entry_note(self, original_entry, note):
        acls_notes.append(f"""
            ACL name: {self._xe_acl_name}
            Original ACL entry: {original_entry}
            {note} 
        """)

    def __set_protocol(self, entry, rule_parts):
        if self._acl_type == "ACL_IPV4_STANDARD":
            return 2
        if rule_parts[2] != 'ip':
            if not rule_parts[2] in protocols_oc_to_xe:
                self.__add_acl_entry_note(" ".join(rule_parts), f"protocol {rule_parts[2]} does not exist in expected list of protocols")
                raise ValueError
            self.__get_ipv4_config(entry)["openconfig-acl:protocol"] = protocols_oc_to_xe[rule_parts[2]]
        
        return 3

    def __get_ipv4_config(self, entry):
        if not self._ipv4_key in entry:
            entry[self._ipv4_key] = {}
        if not self._config_key in entry[self._ipv4_key]:
            entry[self._ipv4_key][self._config_key] = {}
        
        return entry[self._ipv4_key][self._config_key]

    def __get_transport_config(self, entry):
        if not "openconfig-acl:transport" in entry:
            entry["openconfig-acl:transport"] = {}
        if not "openconfig-acl:config" in entry["openconfig-acl:transport"]:
            entry["openconfig-acl:transport"]["openconfig-acl:config"] = {}
        
        return entry["openconfig-acl:transport"]["openconfig-acl:config"]

    def __set_ip_and_port(self, rule_parts, current_index, entry, is_source):
        if len(rule_parts) <= current_index:
            return current_index

        current_index = self.__set_ip_and_network(rule_parts, current_index, entry, is_source)

        if rule_parts[2] == "tcp" or rule_parts[2] == "udp":
            current_index = self.__set_port(rule_parts, current_index, entry, is_source)

        return current_index

    def __set_ip_and_network(self, rule_parts, current_index, entry, is_source):
        ip = rule_parts[current_index]

        if ip == "any":
            if is_source:
                self.__get_ipv4_config(entry)[self._src_addr_key] = "0.0.0.0/0"
            else:
                self.__get_ipv4_config(entry)["openconfig-acl:destination-address"] = "0.0.0.0/0"
            
            return current_index + 1
        elif ip == "host":
            if is_source:
                self.__get_ipv4_config(entry)[self._src_addr_key] = f"{rule_parts[current_index + 1]}/32"
            else:
                self.__get_ipv4_config(entry)["openconfig-acl:destination-address"] = f"{rule_parts[current_index + 1]}/32"
            
            return current_index + 2

        hostmask = rule_parts[current_index + 1]
        temp_ip = IPv4Network((0, hostmask))

        if is_source:
            self.__get_ipv4_config(entry)[self._src_addr_key] = f"{ip}/{temp_ip.prefixlen}"
        else:
            self.__get_ipv4_config(entry)["openconfig-acl:destination-address"] = f"{ip}/{temp_ip.prefixlen}"
        
        return current_index + 2

    def __set_port(self, rule_parts, current_index, entry, is_source):
        if len(rule_parts) <= current_index or not rule_parts[current_index] in port_operators:
            # We've either reached the end of the rule or there's no specified port
            if is_source:
                self.__get_transport_config(entry)["openconfig-acl:source-port"] = "ANY"
            else:
                self.__get_transport_config(entry)["openconfig-acl:destination-port"] = "ANY"
                current_index = self.__set_tcp_flags(rule_parts, current_index, entry)
            
            return current_index
        
        current_port = rule_parts[current_index + 1]

        try:
            current_port = current_port if current_port.isdigit() else socket.getservbyname(current_port)
        except Exception as err:
            self.__add_acl_entry_note(" ".join(rule_parts), f"Unable to convert service {current_port} to a port number")
            raise Exception

        if rule_parts[current_index] == "range":
            end_port = rule_parts[current_index + 2]

            if is_source:
                self.__get_transport_config(entry)["openconfig-acl:source-port"] = f"{current_port}..{end_port}"
            else:
                self.__get_transport_config(entry)["openconfig-acl:destination-port"] = f"{current_port}..{end_port}"
                current_index = self.__set_tcp_flags(rule_parts, current_index + 3, entry)
            
            return current_index
        elif rule_parts[current_index] == "lt":
            if is_source:
                self.__get_transport_config(entry)["openconfig-acl:source-port"] = f"0..{int(current_port) - 1}"
            else:
                self.__get_transport_config(entry)["openconfig-acl:destination-port"] = f"0..{int(current_port) - 1}"
        elif rule_parts[current_index] == "gt":
            if is_source:
                self.__get_transport_config(entry)["openconfig-acl:source-port"] = f"{int(current_port) + 1}..65535"
            else:
                self.__get_transport_config(entry)["openconfig-acl:destination-port"] = f"{int(current_port) + 1}..65535"
        elif rule_parts[current_index] == "eq":
            if is_source:
                self.__get_transport_config(entry)["openconfig-acl:source-port"] = int(current_port)
            else:
                self.__get_transport_config(entry)["openconfig-acl:destination-port"] = int(current_port)
        elif rule_parts[current_index] == "neq":
            self.__add_acl_entry_note(" ".join(rule_parts), "XE ACL use of 'neq' port operator does not have an OC equivalent.")
            raise ValueError
        
        if not is_source:
            current_index = self.__set_tcp_flags(rule_parts, current_index + 2, entry)

        return current_index + 2

    def __set_tcp_flags(self, rule_parts, current_index, entry):
        if len(rule_parts) <= current_index or not rule_parts[current_index] in ["ack", "rst", "established"]:
            return current_index

        if rule_parts[current_index] == "ack":
            self.__get_transport_config(entry)["openconfig-acl:tcp-flags"] = ["TCP_ACK"]
        if rule_parts[current_index] == "rst":
            self.__get_transport_config(entry)["openconfig-acl:tcp-flags"] = ["TCP_RST"]
        if rule_parts[current_index] == "established":
            self.__get_transport_config(entry)["openconfig-acl:tcp-flags"] = ["TCP_ACK", "TCP_RST"]

        return current_index + 1

class StandardAcl(BaseAcl):
    def __init__(self, oc_acl_set, xe_acl_set, xe_acl_set_after):
        super(StandardAcl, self).__init__(oc_acl_set, xe_acl_set, xe_acl_set_after)
        self._rule_list_key = "std-access-list-rule"
        self._acl_type = "ACL_IPV4_STANDARD"
        self._ipv4_key = "openconfig-acl-ext:ipv4"
        self._config_key = "openconfig-acl-ext:config"
        self._src_addr_key = "openconfig-acl-ext:source-address"

class ExtendedAcl(BaseAcl):
    def __init__(self, oc_acl_set, xe_acl_set, xe_acl_set_after):
        super(ExtendedAcl, self).__init__(oc_acl_set, xe_acl_set, xe_acl_set_after)
        self._rule_list_key = "ext-access-list-rule"
        self._acl_type = "ACL_IPV4"
        self._ipv4_key = "openconfig-acl:ipv4"
        self._config_key = "openconfig-acl:config"
        self._src_addr_key = "openconfig-acl:source-address"

def get_interfaces_by_acl(config_before, config_after):
    interfaces_by_acl = {}
    interfaces = config_before.get("tailf-ned-cisco-ios:interface", {})
    interfaces_after = config_after.get("tailf-ned-cisco-ios:interface", {})
    for interface_type, interface_list in interfaces.items():
        interface_list_after = interfaces_after[interface_type]

        if interface_type == "Port-channel-subinterface":
            interface_type = "Port-channel"
            interface_list = interface_list[interface_type]
            interface_list_after = interface_list_after[interface_type]

        for index, interface in enumerate(interface_list):
            if not "ip" in interface or not "access-group" in interface["ip"] or len(interface["ip"]["access-group"]) < 1:
                continue

            intf_id = f"{interface_type}{interface['name']}"
            intf_numb_parts = re.split("[\./]", interface["name"])
            intf_num = intf_numb_parts[0]
            subintf_num = int(intf_numb_parts[1]) if len(intf_numb_parts) > 1 else 0

            for access_group in interface["ip"]["access-group"]:
                if interface_list_after[index].get("ip") and interface_list_after[index]["ip"].get("access-group"):
                    del interface_list_after[index]["ip"]["access-group"]
                
                intf = {
                    "id": intf_id,
                    "interface": f"{interface_type}{intf_num}",
                    "subinterface": subintf_num,
                    "direction": access_group["direction"]
                }
                
                if not access_group["access-list"] in interfaces_by_acl:
                    interfaces_by_acl[access_group["access-list"]] = []

                interfaces_by_acl[access_group["access-list"]].append(intf)
                
    return interfaces_by_acl

def process_interfaces(acl_type, acl_name, interfaces_by_acl, acl_interfaces):
    interfaces = interfaces_by_acl.get(acl_name, [])
    
    for interface in interfaces:
        if interface["id"] in acl_interfaces:
            acl_interface = acl_interfaces[interface["id"]]
        else:
            acl_interface = {
                "openconfig-acl:id": interface["id"],
                "openconfig-acl:config": {"openconfig-acl:id": interface["id"]},
                "openconfig-acl:interface-ref": {
                    "openconfig-acl:config": {
                        "openconfig-acl:interface": interface["interface"],
                        "openconfig-acl:subinterface": interface["subinterface"]
                    }
                }
            }
            acl_interfaces[interface["id"]] = acl_interface
        
        intf_acl_set = get_intf_acl_set(acl_interface, interface["direction"])
        intf_acl_set.append({
            "openconfig-acl:set-name": acl_name,
            "openconfig-acl:type": acl_type,
            "openconfig-acl:config": {
                "openconfig-acl:set-name": acl_name,
                "openconfig-acl:type": acl_type
            }
        })

def get_intf_acl_set(acl_interface, direction):
    if direction == "in":
        ingress_set = "openconfig-acl:ingress-acl-set"
        if not f"{ingress_set}s" in acl_interface:
            acl_interface[f"{ingress_set}s"] = {ingress_set: []}
        if not ingress_set in acl_interface[f"{ingress_set}s"]:
            acl_interface[f"{ingress_set}s"][ingress_set] = []
        
        return acl_interface[f"{ingress_set}s"][ingress_set]
    else:
        egress_set = "openconfig-acl:egress-acl-set"
        if not f"{egress_set}s" in acl_interface:
            acl_interface[f"{egress_set}s"] = {egress_set: []}
        if not egress_set in acl_interface[f"{egress_set}s"]:
            acl_interface[f"{egress_set}s"][egress_set] = []
        
        return acl_interface[f"{egress_set}s"][egress_set]

def process_ntp(config_before, config_after):
    ntp_access_group = config_before.get("tailf-ned-cisco-ios:ntp", {}).get("access-group", {})
    ntp_access_group_after = config_after.get("tailf-ned-cisco-ios:ntp", {}).get("access-group", {})

    if ntp_access_group.get("serve") and ntp_access_group["serve"].get("access-list"):
        openconfig_acls["openconfig-acl:acl"]["openconfig-acl-ext:ntp"] = {
            "openconfig-acl-ext:server": {
                "openconfig-acl-ext:config": {
                    "openconfig-acl-ext:server-acl-set": ntp_access_group["serve"]["access-list"]
                }
            }
        }
        del ntp_access_group_after["serve"]["access-list"]
    if ntp_access_group.get("peer") and ntp_access_group["peer"].get("access-list"):
        ntp_peer = {
            "openconfig-acl-ext:peer": {
                "openconfig-acl-ext:config": {
                    "openconfig-acl-ext:peer-acl-set": ntp_access_group["peer"]["access-list"]
                }
            }
        }

        if openconfig_acls["openconfig-acl:acl"].get("openconfig-acl-ext:ntp"):
            openconfig_acls["openconfig-acl:acl"]["openconfig-acl-ext:ntp"].update(ntp_peer)
        else:
            openconfig_acls["openconfig-acl:acl"]["openconfig-acl-ext:ntp"] = ntp_peer

        del ntp_access_group_after["peer"]["access-list"]

def process_line(config_before, config_after):
    vty_accesses = config_before.get("tailf-ned-cisco-ios:line", {}).get("vty")
    vty_accesses_after = config_after.get("tailf-ned-cisco-ios:line", {}).get("vty")
    openconfig_acls["openconfig-acl:acl"]["openconfig-acl-ext:lines"] = {"openconfig-acl-ext:line": []}
    acl_line = openconfig_acls["openconfig-acl:acl"]["openconfig-acl-ext:lines"]["openconfig-acl-ext:line"]

    for index, access in enumerate(vty_accesses):
        line_item = {
            "openconfig-acl-ext:id": f"vty {access['first']} {access['last']}",
            "openconfig-acl-ext:config": {
                "openconfig-acl-ext:id": f"vty {access['first']} {access['last']}"
            }
        }
        if ("access-class" in access and "access-list" in access["access-class"]) or (
            "access-class-vrf" in access and "access-class" in access["access-class-vrf"]):
            acl_line.append(line_item)

        if "access-class" in access and "access-list" in access["access-class"]:
            process_vrf(access["access-class"]["access-list"], line_item)
            vty_accesses_after[index]["access-class"]["access-list"] = None
        elif "access-class-vrf" in access and "access-class" in access["access-class-vrf"]:
            process_vrf(access["access-class-vrf"]["access-class"], line_item)
            vty_accesses_after[index]["access-class-vrf"]["access-class"] = None

def process_vrf(access_list, line_item):
    for access in access_list:
        if access["direction"] == "out":
            line_item["openconfig-acl-ext:egress-acl-set"] = access["access-list"]
        else:
            line_item["openconfig-acl-ext:ingress-acl-sets"] = {
                "openconfig-acl-ext:ingress-acl-set": [
                    {
                        "openconfig-acl-ext:ingress-acl-set-name": access["access-list"],
                        "openconfig-acl-ext:config": {
                            "openconfig-acl-ext:vrf": access["vrfname"] if "vrfname" in access else "global",
                            "openconfig-acl-ext:vrf-also": "vrf-also" in access,
                            "openconfig-acl-ext:ingress-acl-set-name": access["access-list"]
                        }
                    }
                ]
            }

def main(before: dict, leftover: dict, translation_notes: list = []) -> dict:
    """
    Translates NSO Device configurations to MDD OpenConfig configurations.

    Requires environment variables:
    NSO_HOST: str
    NSO_USERNAME: str
    NSO_PASSWORD: str
    NSO_DEVICE: str
    TEST - If True, sends generated OC configuration to NSO Server: str

    :param before: Original NSO Device configuration: dict
    :param leftover: NSO Device configuration minus configs replaced with MDD OC: dict
    :return: MDD Openconfig Network Instances configuration: dict
    """

    xe_acls(before, leftover)
    translation_notes += acls_notes

    return openconfig_acls

if __name__ == "__main__":
    sys.path.append("../../")
    sys.path.append("../../../")

    if (find_spec("package_nso_to_oc") is not None):
        from package_nso_to_oc.xe import common_xe
        from package_nso_to_oc import common
    else:
        import common_xe
        import common

    (config_before_dict, config_leftover_dict, interface_ip_dict) = common_xe.init_xe_configs()
    main(config_before_dict, config_leftover_dict)
    config_name = "ned_configuration_acls"
    config_remaining_name = "ned_configuration_remaining_acls"
    oc_name = "openconfig_acls"
    common.print_and_test_configs(
        "xe1", config_before_dict, config_leftover_dict, openconfig_acls, 
        config_name, config_remaining_name, oc_name, acls_notes)
else:
    # This is needed for now due to top level __init__.py. We need to determine if contents in __init__.py is still necessary.
    if (find_spec("package_nso_to_oc") is not None):
        from package_nso_to_oc.xe import common_xe
        from package_nso_to_oc import common
    else:
        from xe import common_xe
        import common
