import requests
import time

import json
import struct

from brewpiv2.commands import (
    InstallDeviceCommand,
    ControlSettingsCommand,
    WriteDeviceCommand
)

from brewpiv2.constants import (
    HardwareType, DeviceType,
    DeviceAssignation, DeviceFunction, DeviceState
)

from flask import Flask
from flask_apispec import MethodResource
from flask_apispec import FlaskApiSpec
from flask_apispec import use_kwargs
from flask_apispec.annotations import doc
from flask_cors import CORS
from flask import jsonify
from flask.ext.cache import Cache

from webargs import fields

from werkzeug.exceptions import BadRequest

(CONFIG_OFF,
 CONFIG_PREHEAT,
 CONFIG_MASH,
 CONFIG_BOIL,
 CONFIG_RESERVOIR) = range(0, 5)

config_name_mapping = {
    CONFIG_OFF: 'off',
    CONFIG_PREHEAT: 'preheat',
    CONFIG_MASH: 'mash',
    CONFIG_BOIL: 'boil',
    CONFIG_RESERVOIR: 'reservoir'
}


app = Flask("brewmonkey", instance_relative_config=True)
app.config.from_envvar('BREWMONKEY_SETTINGS')

cache = Cache(app, config={'CACHE_TYPE': 'simple'})


CORS(app)

docs = FlaskApiSpec(app)

class ArduinoHLT:
    def __init__(self, host, username=None, password=None):
        self._host = host
        self._uri = "http://{0}".format(host)

        self.session = requests.Session()
        if username:
            self.session.auth = (username, password)

    @cache.cached(timeout=1)
    def get_status(self):
        r = self.session.get("{0}/status".format(self._uri))

        r.raise_for_status()

        return r.json()

    def fill_to(self, target):
        print(target)
        r = self.session.get("{0}/fill?target={1}".format(self._uri, target))

        r.raise_for_status()

        return None


class BrewpiSocketMessage:
    def __init__(self, host, username=None, password=None):
        self._host = host
        self._uri = "{0}/socketmessage.php".format(host)
        self._data_uri = "{0}/get_beer_data.php".format(host)

        self.session = requests.Session()
        if username:
            self.session.auth = (username, password)

    @cache.cached(timeout=1)
    def get_temperatures(self):
        r = self.session.post(self._uri, data={'messageType': 'getTemperatures'})

        r.raise_for_status()

        return r.json()

    def set_fridge_setpoint(self, setpoint):
        r = self.session.post(self._uri, data={'messageType': 'setFridge', 'message': setpoint})
        r.raise_for_status()
        return r.text

    def set_beer_setpoint(self, setpoint):
        r = self.session.post(self._uri, data={'messageType': 'setBeer', 'message': setpoint})
        r.raise_for_status()
        return r.text

    @cache.cached(timeout=1)
    def get_log(self):
        r = self.session.post(self._data_uri)

        r.raise_for_status()

        return r.text

    def set_device_state(self, aDeviceStateCommand):
        r = self.session.post(self._uri, data={'messageType': 'writeDevice', 'message': aDeviceStateCommand.render(with_quotes=True, parameters_only=True)})
        r.raise_for_status()

        return r.text

    def reset_controller(self):
        for i in range(0, 10):
            self.set_device_state(WriteDeviceCommand(slot=i, state=DeviceState.STATE_OFF))

        time.sleep(2)

        r = self.session.post(self._uri, data={'messageType': 'resetController'})

        time.sleep(2)

        r = self.session.post(self._uri, data={'messageType': 'setOff'})

        r.raise_for_status()

        return r.text


    def configure(self, aControllerCommand):
        r = self.session.post(self._uri, data={'messageType': 'applyDevice',
                                               'message': aControllerCommand.render(with_quotes=True, parameters_only=True)})

        r.raise_for_status()

        return r.text

    def get_control_constants(self):
        r = self.session.post(self._uri, data={'messageType': 'getControlConstants', 'message': ''})
        r.raise_for_status()

        return r.json()

    def set_control_constants(self, aControlSettingsCommand):
        r = self.session.post(self._uri, data={'messageType': 'setParameters',
                                               'message': aControlSettingsCommand.render(with_quotes=True, parameters_only=True)})
        r.raise_for_status()

        return r.text


# FIXME: global
transport = BrewpiSocketMessage(app.config["BREWPI_URI"], username=app.config["BREWPI_USERNAME"], password=app.config["BREWPI_PASSWORD"])
transport_hlt = ArduinoHLT(app.config["HLT_HOST"])

class ConfigurationSwitcher:
    def _make_actuator_commands(self, starting_slot, heater_pin, manual_actuators=[]):
        """
        Instanciate actuator installation commands
        """
        slot_number = starting_slot
        cmds = []
        delayed_cmds = []

        heater_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.CHAMBER,
                                          function=DeviceFunction.CHAMBER_HEATER,
                                          hardware_type=HardwareType.DIGITAL_PIN,
                                          pin=heater_pin)
        cmds.append(heater_cmd)
        slot_number += 1

        for manual_actuator_pin in manual_actuators:
            manual_actuator_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.CHAMBER,
                                                       function=DeviceFunction.MANUAL_ACTUATOR,
                                                       hardware_type=HardwareType.DIGITAL_PIN,
                                                       pin=manual_actuator_pin)
            cmds.append(manual_actuator_cmd)

            # Turn them on
            delayed_cmds.append(WriteDeviceCommand(slot=slot_number, state=DeviceState.STATE_ON))
            slot_number += 1

        return (cmds, delayed_cmds, slot_number)

    def _make_sensor_commands(self, starting_slot, chamber_sensor_address, beer_sensor_address=None,
                              log1_sensor_address=None, log2_sensor_address=None, log3_sensor_address=None):
        """
        Instanciate sensor installation commands
        """
        slot_number = starting_slot
        cmds = []

        # Beer Sensor
        if beer_sensor_address:
            beer_sensor_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.BEER,
                                                   function=DeviceFunction.BEER_TEMP,
                                                   hardware_type=HardwareType.TEMP_SENSOR,
                                                   address=beer_sensor_address)
            beer_sensor_cmd.options['c'] = 1

            cmds.append(beer_sensor_cmd)
            slot_number += 1


        # Chamber Sensor
        chamber_sensor_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.CHAMBER,
                                                  function=DeviceFunction.CHAMBER_TEMP,
                                                  hardware_type=HardwareType.TEMP_SENSOR,
                                                  address=chamber_sensor_address)
        cmds.append(chamber_sensor_cmd)
        slot_number += 1

        # Extra log sensors
        if log1_sensor_address is not None:
            log1_sensor_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.CHAMBER,
                                                   function=DeviceFunction.LOG1_TEMP,
                                                   hardware_type=HardwareType.TEMP_SENSOR,
                                                   address=log1_sensor_address)
            cmds.append(log1_sensor_cmd)
            slot_number += 1


        if log2_sensor_address is not None:
            log2_sensor_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.CHAMBER,
                                                   function=DeviceFunction.LOG2_TEMP,
                                                   hardware_type=HardwareType.TEMP_SENSOR,
                                                   address=log2_sensor_address)
            cmds.append(log2_sensor_cmd)
            slot_number += 1


        if log3_sensor_address is not None:
            log3_sensor_cmd = InstallDeviceCommand(slot=slot_number, assigned_to=DeviceAssignation.CHAMBER,
                                                   function=DeviceFunction.LOG3_TEMP,
                                                   hardware_type=HardwareType.TEMP_SENSOR,
                                                   address=log3_sensor_address)
            cmds.append(log3_sensor_cmd)
            slot_number += 1

        return (cmds, slot_number)


    def switch_to_off(self):
        """
        Stop the brewery
        """
        # Execute them
        self._execute_configuration_switch(device_commands=[],
                                           control_cmds=[],
                                           delayed_cmds=[],
                                           configuration_code=CONFIG_OFF)


    def switch_to_preheating(self):
        """
        Switch to preheating configuration
        """
        next_free_slot_number = 0
        device_cmds = []
        delayed_cmds = []

        # Create commands : sensors, actuators
        (new_cmds, next_free_slot_number) = self._make_sensor_commands(starting_slot=next_free_slot_number,
                                                                       chamber_sensor_address=app.config['SENSOR_HLT'],
                                                                       beer_sensor_address=app.config['SENSOR_MASHTUN'],
                                                                       log1_sensor_address=app.config['SENSOR_RES'])
        device_cmds += new_cmds

        (new_cmds, delayed_cmds, next_free_slot_number) = self._make_actuator_commands(starting_slot=next_free_slot_number,
                                                                                       heater_pin=app.config['ACTUATOR_HLT'],
                                                                                       manual_actuators=[])

        device_cmds += new_cmds

        control_cmds = [ControlSettingsCommand(beer2fridge_kp=1.5, beer2fridge_ti=600, beer2fridge_td=0, beer2fridge_maxdif=4,
                                               heater1_kp=50, heater1_ti=300, heater1_td=30)]

        # Execute them
        self._execute_configuration_switch(device_commands=device_cmds,
                                           control_cmds=control_cmds,
                                           delayed_cmds=delayed_cmds,
                                           configuration_code=CONFIG_PREHEAT)


    def switch_to_reservoir(self):
        """
        Switch to reservoir configuration
        """
        next_free_slot_number = 0
        device_cmds = []
        delayed_cmds = []

        # Create commands : sensors, actuators
        (new_cmds, next_free_slot_number) = self._make_sensor_commands(starting_slot=next_free_slot_number,
                                                                       chamber_sensor_address=app.config['SENSOR_RES'],
                                                                       log1_sensor_address=app.config['SENSOR_MASHTUN'],
                                                                       log2_sensor_address=app.config['SENSOR_BK'],
                                                                       log3_sensor_address=app.config['SENSOR_CFC'])
        device_cmds += new_cmds

        (new_cmds, delayed_cmds, next_free_slot_number) = self._make_actuator_commands(starting_slot=next_free_slot_number,
                                                                                       heater_pin=app.config['ACTUATOR_RES'],
                                                                                       manual_actuators=[])

        device_cmds += new_cmds

        # Execute them
        self._execute_configuration_switch(device_commands=device_cmds,
                                           control_cmds=[],
                                           delayed_cmds=delayed_cmds,
                                           configuration_code=CONFIG_RESERVOIR)



    def switch_to_mashing(self):
        """
        Switch to mash configuration
        """
        next_free_slot_number = 0
        device_cmds = []
        delayed_cmds = []

        # Create commands : sensors, actuators
        (new_cmds, next_free_slot_number) = self._make_sensor_commands(starting_slot=next_free_slot_number,
                                                                       chamber_sensor_address=app.config['SENSOR_HLT'],
                                                                       beer_sensor_address=app.config['SENSOR_MASHTUN'],
                                                                       log1_sensor_address=app.config['SENSOR_RES'])
        device_cmds += new_cmds

        (new_cmds, delayed_cmds, next_free_slot_number) = self._make_actuator_commands(starting_slot=next_free_slot_number,
                                                                                       heater_pin=app.config['ACTUATOR_HLT'],
                                                                                       manual_actuators=[app.config['ACTUATOR_RES']])

        device_cmds += new_cmds

        control_cmds = [ControlSettingsCommand(beer2fridge_kp=1.5, beer2fridge_ti=600, beer2fridge_td=0, beer2fridge_maxdif=5,
                                               heater1_kp=50, heater1_ti=300, heater1_td=30)]

        # Execute them
        self._execute_configuration_switch(device_commands=device_cmds,
                                           control_cmds=control_cmds,
                                           delayed_cmds=delayed_cmds,
                                           configuration_code=CONFIG_PREHEAT)

    def switch_to_boil(self):
        """
        Switch to boil configuration
        """
        next_free_slot_number = 0
        device_cmds = []
        delayed_cmds = []

        # Create commands : sensors, actuators
        (new_cmds, next_free_slot_number) = self._make_sensor_commands(starting_slot=next_free_slot_number,
                                                                       chamber_sensor_address=app.config['SENSOR_BK'],
                                                                       log1_sensor_address=app.config['SENSOR_RES'],
                                                                       log2_sensor_address=app.config['SENSOR_HLT'],
                                                                       log3_sensor_address=app.config['SENSOR_CFC'])
        device_cmds += new_cmds

        (new_cmds, delayed_cmds, next_free_slot_number) = self._make_actuator_commands(starting_slot=next_free_slot_number,
                                                                                       heater_pin=app.config['ACTUATOR_BK'],
                                                                                       manual_actuators=[app.config['ACTUATOR_RES'], app.config['ACTUATOR_HLT']])

        device_cmds += new_cmds

        # Control Settings
        control_cmds = [ControlSettingsCommand(heater1_kp=30, heater1_ti=0, heater1_td=0, heater1_pwm_period=2)]

        # Execute them
        self._execute_configuration_switch(device_commands=device_cmds,
                                           control_cmds=control_cmds,
                                           delayed_cmds=delayed_cmds,
                                           configuration_code=CONFIG_BOIL)


    def _execute_configuration_switch(self, device_commands, control_cmds, delayed_cmds, configuration_code):
        """
        Execute commands, resetting controller first
        """
        # Reset all settings
        transport.reset_controller()

        # execute device installation commands
        for cmd in device_commands:
            transport.configure(cmd)

        for cmd in control_cmds:
            transport.set_control_constants(cmd)

        for cmd in delayed_cmds:
            transport.set_device_state(cmd)

        # Save the configuration we're now in
        configuration_settings_cmd = ControlSettingsCommand(heater2_kp=configuration_code)
        transport.set_control_constants(configuration_settings_cmd)


@doc(description='HLT')
class HLT(MethodResource):
    """
    State and operations on the HLT
    """
    def get(self):
        response = transport_hlt.get_status()
        return response

    @use_kwargs({'target_liters': fields.Integer()})
    def post(self, target_liters):
        return transport_hlt.fill_to(target_liters)

    @use_kwargs({'amount': fields.Float()})
    def delete(self, amount):
        return transport_hlt.post('transfer', amount)

app.add_url_rule('/hlt', view_func=HLT.as_view('hlt'))
docs.register(HLT)

@doc(description='Temp')
class BrewPiTemp(MethodResource):
    """
    Read/Write temperature on the BrewPi
    """
    def get(self):
        return transport.get_temperatures()

    @use_kwargs({'fridge': fields.Float()})
    @use_kwargs({'beer': fields.Float()})
    def post(self, beer=None, fridge=None):
        if beer:
            return transport.set_beer_setpoint(beer)
        elif fridge:
            return transport.set_fridge_setpoint(fridge)


app.add_url_rule('/temp', view_func=BrewPiTemp.as_view('brewpitemp'))
docs.register(BrewPiTemp)

@doc(description='Log')
class BrewPiLog(MethodResource):
    """
    Read Log from the BrewPi
    """
    def get(self):
        return transport.get_log()

app.add_url_rule('/log', view_func=BrewPiLog.as_view('brewpilog'))
docs.register(BrewPiLog)



@doc(description='Configuration Switcher')
class BrewPiConfigurationSwitcher(MethodResource):
    """
    Switches configuration on the BrewPi
    """
    def get(self):
        """
        This is a hack. We use the unused "heater2_kp" value to guess the state we're running
        """
        data = transport.get_control_constants()
        current_configuration = int(data['heater2_kp'])
        if current_configuration in config_name_mapping:
            return jsonify({'configuration': config_name_mapping[current_configuration]})
        else:
            return jsonify({'configuration': -1})

    @use_kwargs({'name': fields.Str(required=True)})
    def post(self, name):
        switcher = ConfigurationSwitcher()

        configurations = {
            'off': switcher.switch_to_off,
            'preheat': switcher.switch_to_preheating,
            'mash': switcher.switch_to_mashing,
            'boil': switcher.switch_to_boil,
            'reservoir': switcher.switch_to_reservoir
        }

        if name in configurations:
            configurations[name]()
        else:
            raise BadRequest("No such configuration available")

        return "configuration switched"


app.add_url_rule('/configuration', view_func=BrewPiConfigurationSwitcher.as_view('brewpiconfigurationswitcher'))
docs.register(BrewPiConfigurationSwitcher)
