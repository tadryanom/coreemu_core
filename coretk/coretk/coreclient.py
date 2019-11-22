"""
Incorporate grpc into python tkinter GUI
"""
import logging
import os

from core.api.grpc import client, core_pb2
from coretk.dialogs.sessions import SessionsDialog
from coretk.interface import InterfaceManager
from coretk.nodeutils import NodeDraw, NodeUtils

OBSERVERS = {
    "processes": "ps",
    "ifconfig": "ifconfig",
    "IPV4 Routes": "ip -4 ro",
    "IPV6 Routes": "ip -6 ro",
    "Listening sockets": "netstat -tuwnl",
    "IPv4 MFC entries": "ip -4 mroute show",
    "IPv6 MFC entries": "ip -6 mroute show",
    "firewall rules": "iptables -L",
    "IPSec policies": "setkey -DP",
}


class CoreServer:
    def __init__(self, name, address, port):
        self.name = name
        self.address = address
        self.port = port


class Observer:
    def __init__(self, name, cmd):
        self.name = name
        self.cmd = cmd


class CoreClient:
    def __init__(self, app):
        """
        Create a CoreGrpc instance
        """
        self.client = client.CoreGrpcClient()
        self.session_id = None
        self.node_ids = []
        self.app = app
        self.master = app.master
        self.interface_helper = None
        self.services = {}
        self.default_services = {}
        self.emane_models = []
        self.observer = None

        # loaded configuration data
        self.servers = {}
        self.custom_nodes = {}
        self.custom_observers = {}
        self.read_config()

        # data for managing the current session
        self.canvas_nodes = {}
        self.location = None
        self.interface_to_edge = {}
        self.state = None
        self.links = {}
        self.hooks = {}
        self.id = 1
        self.reusable = []
        self.preexisting = set()
        self.interfaces_manager = InterfaceManager()
        self.wlan_configs = {}
        self.mobility_configs = {}
        self.emane_model_configs = {}
        self.emane_config = None
        self.created_nodes = set()
        self.created_links = set()

        self.service_configs = {}
        self.file_configs = {}

    def set_observer(self, value):
        self.observer = value

    def read_config(self):
        # read distributed server
        for config in self.app.guiconfig.get("servers", []):
            server = CoreServer(config["name"], config["address"], config["port"])
            self.servers[server.name] = server

        # read custom nodes
        for config in self.app.guiconfig.get("nodes", []):
            name = config["name"]
            image_file = config["image"]
            services = set(config["services"])
            node_draw = NodeDraw.from_custom(name, image_file, services)
            self.custom_nodes[name] = node_draw

        # read observers
        for config in self.app.guiconfig.get("observers", []):
            observer = Observer(config["name"], config["cmd"])
            self.custom_observers[observer.name] = observer

    def handle_events(self, event):
        logging.info("event: %s", event)
        if event.HasField("link_event"):
            self.app.canvas.wireless_draw.handle_link_event(event.link_event)
        elif event.HasField("session_event"):
            if event.session_event.event <= core_pb2.SessionState.SHUTDOWN:
                self.state = event.session_event.event

    def handle_throughputs(self, event):
        interface_throughputs = event.interface_throughputs
        for i in interface_throughputs:
            print("")
        return
        throughputs_belong_to_session = []
        for if_tp in interface_throughputs:
            if if_tp.node_id in self.node_ids:
                throughputs_belong_to_session.append(if_tp)
        self.throughput_draw.process_grpc_throughput_event(
            throughputs_belong_to_session
        )

    def join_session(self, session_id, query_location=True):
        self.master.config(cursor="watch")
        self.master.update()

        # update session and title
        self.session_id = session_id
        self.master.title(f"CORE Session({self.session_id})")

        # clear session data
        self.reusable.clear()
        self.preexisting.clear()
        self.canvas_nodes.clear()
        self.links.clear()
        self.hooks.clear()
        self.wlan_configs.clear()
        self.mobility_configs.clear()
        self.emane_config = None
        self.service_configs.clear()
        self.file_configs.clear()

        # get session data
        response = self.client.get_session(self.session_id)
        logging.info("joining session(%s): %s", self.session_id, response)
        session = response.session
        self.state = session.state
        self.client.events(self.session_id, self.handle_events)

        # get location
        if query_location:
            response = self.client.get_session_location(self.session_id)
            self.location = response.location

        # get emane models
        response = self.client.get_emane_models(self.session_id)
        self.emane_models = response.models

        # get hooks
        response = self.client.get_hooks(self.session_id)
        for hook in response.hooks:
            self.hooks[hook.file] = hook

        # get wlan configs
        for node in session.nodes:
            if node.type == core_pb2.NodeType.WIRELESS_LAN:
                response = self.client.get_wlan_config(self.session_id, node.id)
                self.wlan_configs[node.id] = response.config

        # get mobility configs
        response = self.client.get_mobility_configs(self.session_id)
        for node_id in response.configs:
            node_config = response.configs[node_id].config
            self.mobility_configs[node_id] = node_config

        # get emane config
        response = self.client.get_emane_config(self.session_id)
        self.emane_config = response.config

        # get emane model config

        # determine next node id and reusable nodes
        max_id = 1
        for node in session.nodes:
            if node.id > max_id:
                max_id = node.id
            self.preexisting.add(node.id)
        self.id = max_id
        for i in range(1, self.id):
            if i not in self.preexisting:
                self.reusable.append(i)

        # draw session
        self.app.canvas.reset_and_redraw(session)

        # draw tool bar appropritate with session state
        if self.is_runtime():
            self.app.toolbar.runtime_frame.tkraise()
        else:
            self.app.toolbar.design_frame.tkraise()
        self.master.config(cursor="")

    def is_runtime(self):
        return self.state == core_pb2.SessionState.RUNTIME

    def create_new_session(self):
        """
        Create a new session

        :return: nothing
        """
        response = self.client.create_session()
        logging.info("created session: %s", response)
        location_config = self.app.guiconfig["location"]
        self.location = core_pb2.SessionLocation(
            x=location_config["x"],
            y=location_config["y"],
            z=location_config["z"],
            lat=location_config["lat"],
            lon=location_config["lon"],
            alt=location_config["alt"],
            scale=location_config["scale"],
        )
        self.join_session(response.session_id, query_location=False)

    def delete_session(self, custom_sid=None):
        if custom_sid is None:
            sid = self.session_id
        else:
            sid = custom_sid
        response = self.client.delete_session(sid)
        logging.info("Deleted session result: %s", response)

    def shutdown_session(self, custom_sid=None):
        if custom_sid is None:
            sid = self.session_id
        else:
            sid = custom_sid
        s = self.client.get_session(sid).session
        # delete links and nodes from running session
        if s.state == core_pb2.SessionState.RUNTIME:
            self.client.set_session_state(
                self.session_id, core_pb2.SessionState.DATACOLLECT
            )
            self.delete_links(sid)
            self.delete_nodes(sid)
        self.delete_session(sid)

    def set_up(self):
        """
        Query sessions, if there exist any, prompt whether to join one

        :return: existing sessions
        """
        self.client.connect()

        # get service information
        response = self.client.get_services()
        for service in response.services:
            group_services = self.services.setdefault(service.group, set())
            group_services.add(service.name)

        # if there are no sessions, create a new session, else join a session
        response = self.client.get_sessions()
        logging.info("current sessions: %s", response)
        sessions = response.sessions
        if len(sessions) == 0:
            self.create_new_session()
        else:
            dialog = SessionsDialog(self.app, self.app)
            dialog.show()

        response = self.client.get_service_defaults(self.session_id)
        logging.debug("get service defaults: %s", response)
        self.default_services = {
            x.node_type: set(x.services) for x in response.defaults
        }

    def get_session_state(self):
        response = self.client.get_session(self.session_id)
        logging.info("get session: %s", response)
        return response.session.state

    def edit_node(self, node_id, x, y):
        position = core_pb2.Position(x=x, y=y)
        response = self.client.edit_node(self.session_id, node_id, position)
        logging.info("updated node id %s: %s", node_id, response)

    def delete_nodes(self, delete_session=None):
        if delete_session is None:
            sid = self.session_id
        else:
            sid = delete_session
        for node in self.client.get_session(sid).session.nodes:
            response = self.client.delete_node(self.session_id, node.id)
            logging.info("delete nodes %s", response)

    def delete_links(self, delete_session=None):
        if delete_session is None:
            sid = self.session_id
        else:
            sid = delete_session

        for link in self.client.get_session(sid).session.links:
            response = self.client.delete_link(
                self.session_id,
                link.node_one_id,
                link.node_two_id,
                link.interface_one.id,
                link.interface_two.id,
            )
            logging.info("delete links %s", response)

    def start_session(self):
        nodes = [x.core_node for x in self.canvas_nodes.values()]
        links = list(self.links.values())
        wlan_configs = self.get_wlan_configs_proto()
        mobility_configs = self.get_mobility_configs_proto()
        emane_model_configs = self.get_emane_model_configs_proto()
        hooks = list(self.hooks.values())
        service_configs = self.get_service_config_proto()
        file_configs = self.get_service_file_config_proto()
        self.created_links.clear()
        self.created_nodes.clear()
        if self.emane_config:
            emane_config = {x: self.emane_config[x].value for x in self.emane_config}
        else:
            emane_config = None
        response = self.client.start_session(
            self.session_id,
            nodes,
            links,
            self.location,
            hooks,
            emane_config,
            emane_model_configs,
            wlan_configs,
            mobility_configs,
            service_configs,
            file_configs,
        )
        logging.debug("Start session %s, result: %s", self.session_id, response.result)
        print(self.client.get_session(self.session_id))

    def stop_session(self):
        response = self.client.stop_session(session_id=self.session_id)
        logging.debug("coregrpc.py Stop session, result: %s", response.result)

    def launch_terminal(self, node_id):
        response = self.client.get_node_terminal(self.session_id, node_id)
        logging.info("get terminal %s", response.terminal)
        os.system("xterm -e %s &" % response.terminal)

    def save_xml(self, file_path):
        """
        Save core session as to an xml file

        :param str file_path: file path that user pick
        :return: nothing
        """
        response = self.client.save_xml(self.session_id, file_path)
        logging.info("coregrpc.py save xml %s", response)
        self.client.events(self.session_id, self.handle_events)

    def open_xml(self, file_path):
        """
        Open core xml

        :param str file_path: file to open
        :return: session id
        """
        response = self.client.open_xml(file_path)
        logging.debug("open xml: %s", response)
        self.join_session(response.session_id)

    def get_node_service(self, node_id, service_name):
        response = self.client.get_node_service(self.session_id, node_id, service_name)
        logging.debug("get node service %s", response)
        return response.service

    def set_node_service(self, node_id, service_name, startups, validations, shutdowns):
        response = self.client.set_node_service(
            self.session_id, node_id, service_name, startups, validations, shutdowns
        )
        logging.debug("set node service %s", response)
        response = self.client.get_node_service(self.session_id, node_id, service_name)
        logging.debug("get node service : %s", response)
        return response.service

    def get_node_service_file(self, node_id, service_name, file_name):
        response = self.client.get_node_service_file(
            self.session_id, node_id, service_name, file_name
        )
        logging.debug("get service file %s", response)
        return response.data

    def set_node_service_file(self, node_id, service_name, file_name, data):
        response = self.client.set_node_service_file(
            self.session_id, node_id, service_name, file_name, data
        )
        logging.debug("set node service file %s", response)

    def create_nodes_and_links(self):
        """
        create nodes and links that have not been created yet

        :return: nothing
        """
        node_protos = [x.core_node for x in self.canvas_nodes.values()]
        link_protos = list(self.links.values())
        print(node_protos)
        if self.get_session_state() != core_pb2.SessionState.DEFINITION:
            self.client.set_session_state(
                self.session_id, core_pb2.SessionState.DEFINITION
            )
        for node_proto in node_protos:
            if node_proto.id not in self.created_nodes:
                response = self.client.add_node(self.session_id, node_proto)
                logging.debug("create node: %s", response)
                self.created_nodes.add(node_proto.id)
        for link_proto in link_protos:
            if (
                tuple([link_proto.node_one_id, link_proto.node_two_id])
                not in self.created_links
            ):
                response = self.client.add_link(
                    self.session_id,
                    link_proto.node_one_id,
                    link_proto.node_two_id,
                    link_proto.interface_one,
                    link_proto.interface_two,
                    link_proto.options,
                )
                logging.debug("create link: %s", response)
                self.created_links.add(
                    tuple([link_proto.node_one_id, link_proto.node_two_id])
                )
        print(self.app.core.client.get_session(self.app.core.session_id))

    def close(self):
        """
        Clean ups when done using grpc

        :return: nothing
        """
        logging.debug("Close grpc")
        self.client.close()

    def get_id(self):
        """
        Get the next node id as well as update id status and reusable ids

        :rtype: int
        :return: the next id to be used
        """
        if len(self.reusable) == 0:
            new_id = self.id
            self.id = self.id + 1
            return new_id
        else:
            return self.reusable.pop(0)

    def create_node(self, x, y, node_type, model):
        """
        Add node, with information filled in, to grpc manager

        :param int x: x coord
        :param int y: y coord
        :param core_pb2.NodeType node_type: node type
        :param str model: node model
        :return: nothing
        """
        node_id = self.get_id()
        position = core_pb2.Position(x=x, y=y)
        image = None
        if NodeUtils.is_image_node(node_type):
            image = "ubuntu:latest"
        emane = None
        if node_type == core_pb2.NodeType.EMANE:
            emane = self.emane_models[0]
        node = core_pb2.Node(
            id=node_id,
            type=node_type,
            name=f"n{node_id}",
            model=model,
            position=position,
            image=image,
            emane=emane,
        )
        logging.debug(
            "adding node to core session: %s, coords: (%s, %s), name: %s",
            self.session_id,
            x,
            y,
            node.name,
        )
        return node

    def delete_wanted_graph_nodes(self, node_ids, edge_tokens):
        """
        remove the nodes selected by the user and anything related to that node
        such as link, configurations, interfaces

        :param list[int] node_ids: list of nodes to delete
        :param list edge_tokens: list of edges to delete
        :return: nothing
        """
        # delete the nodes
        for i in node_ids:
            try:
                del self.canvas_nodes[i]
                self.reusable.append(i)
                if i in self.mobility_configs:
                    del self.mobility_configs[i]
                if i in self.wlan_configs:
                    del self.wlan_configs[i]
                for key in list(self.emane_model_configs):
                    node_id, _, _ = key
                    if node_id == i:
                        del self.emane_model_configs[key]
            except KeyError:
                logging.error("invalid canvas id: %s", i)
        self.reusable.sort()

        # delete the edges and interfaces
        for i in edge_tokens:
            try:
                self.links.pop(i)
            except KeyError:
                logging.error("invalid edge token: %s", i)

    def create_interface(self, canvas_node):
        interface = None
        core_node = canvas_node.core_node
        if NodeUtils.is_container_node(core_node.type):
            ifid = len(canvas_node.interfaces)
            name = f"eth{ifid}"
            interface = core_pb2.Interface(
                id=ifid,
                name=name,
                ip4=str(self.interfaces_manager.get_address()),
                ip4mask=24,
            )
            canvas_node.interfaces.append(interface)
            logging.debug(
                "create node(%s) interface IPv4: %s, name: %s",
                core_node.name,
                interface.ip4,
                interface.name,
            )
        return interface

    def create_link(self, token, canvas_node_one, canvas_node_two):
        """
        Create core link for a pair of canvas nodes, with token referencing
        the canvas edge.

        :param tuple(int, int) token: edge's identification in the canvas
        :param canvas_node_one: canvas node one
        :param canvas_node_two: canvas node two

        :return: nothing
        """
        node_one = canvas_node_one.core_node
        node_two = canvas_node_two.core_node

        # create interfaces
        self.interfaces_manager.new_subnet()
        interface_one = self.create_interface(canvas_node_one)
        if interface_one is not None:
            self.interface_to_edge[(node_one.id, interface_one.id)] = token
        interface_two = self.create_interface(canvas_node_two)
        if interface_two is not None:
            self.interface_to_edge[(node_two.id, interface_two.id)] = token

        link = core_pb2.Link(
            type=core_pb2.LinkType.WIRED,
            node_one_id=node_one.id,
            node_two_id=node_two.id,
            interface_one=interface_one,
            interface_two=interface_two,
        )
        self.links[token] = link
        return link

    def get_wlan_configs_proto(self):
        configs = []
        for node_id, config in self.wlan_configs.items():
            config = {x: config[x].value for x in config}
            wlan_config = core_pb2.WlanConfig(node_id=node_id, config=config)
            configs.append(wlan_config)
        return configs

    def get_mobility_configs_proto(self):
        configs = []
        for node_id, config in self.mobility_configs.items():
            config = {x: config[x].value for x in config}
            mobility_config = core_pb2.MobilityConfig(node_id=node_id, config=config)
            configs.append(mobility_config)
        return configs

    def get_emane_model_configs_proto(self):
        configs = []
        for key, config in self.emane_model_configs.items():
            node_id, model, interface = key
            config = {x: config[x].value for x in config}
            config_proto = core_pb2.EmaneModelConfig(
                node_id=node_id, interface_id=interface, model=model, config=config
            )
            configs.append(config_proto)
        return configs

    def get_service_config_proto(self):
        configs = []
        for node_id, services in self.service_configs.items():
            for name, config in services.items():
                config_proto = core_pb2.ServiceConfig(
                    node_id=node_id,
                    service=name,
                    startup=config.startup,
                    validate=config.validate,
                    shutdown=config.shutdown,
                )
                configs.append(config_proto)
        return configs

    def get_service_file_config_proto(self):
        configs = []
        for (node_id, file_configs) in self.file_configs.items():
            for service, file_config in file_configs.items():
                for file, data in file_config.items():
                    config_proto = core_pb2.ServiceFileConfig(
                        node_id=node_id, service=service, file=file, data=data
                    )
                    configs.append(config_proto)
        return configs

    def run(self, node_id):
        logging.info("running node(%s) cmd: %s", node_id, self.observer)
        return self.client.node_command(self.session_id, node_id, self.observer).output

    def get_wlan_config(self, node_id):
        config = self.wlan_configs.get(node_id)
        if not config:
            response = self.client.get_wlan_config(self.session_id, node_id)
            config = response.config
        return config

    def get_mobility_config(self, node_id):
        config = self.mobility_configs.get(node_id)
        if not config:
            response = self.client.get_mobility_config(self.session_id, node_id)
            config = response.config
        return config

    def get_emane_model_config(self, node_id, model, interface=None):
        config = self.emane_model_configs.get((node_id, model, interface))
        if not config:
            if interface is None:
                interface = -1
            response = self.client.get_emane_model_config(
                self.session_id, node_id, model, interface
            )
            config = response.config
        return config

    def set_emane_model_config(self, node_id, model, config, interface=None):
        self.emane_model_configs[(node_id, model, interface)] = config
