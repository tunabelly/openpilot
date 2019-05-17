#!/usr/bin/env python
from cereal import car
from common.realtime import sec_since_boot
from common.numpy_fast import interp
from selfdrive.config import Conversions as CV
from selfdrive.controls.lib.drive_helpers import create_event, EventTypes as ET
from selfdrive.controls.lib.vehicle_model import VehicleModel
from selfdrive.car.gm.values import DBC, CAR, STOCK_CONTROL_MSGS, AUDIO_HUD, SUPERCRUISE_CARS
from selfdrive.car.gm.carstate import CarState, CruiseButtons, get_powertrain_can_parser, get_chassis_can_parser

try:
  from selfdrive.car.gm.carcontroller import CarController
except ImportError:
  CarController = None

FOLLOW_AGGRESSION = 0.15 # (Acceleration/Decel aggression) Lower is more aggressive

class CanBus(object):
  def __init__(self):
    self.powertrain = 0
    self.obstacle = 1
    self.chassis = 2
    self.sw_gmlan = 3

class CarInterface(object):
  def __init__(self, CP, sendcan=None):
    self.CP = CP

    self.frame = 0
    self.gas_pressed_prev = False
    self.brake_pressed_prev = False
    self.can_invalid_count = 0
    self.acc_active_prev = 0

    # *** init the major players ***
    canbus = CanBus()
    self.CS = CarState(CP, canbus)
    self.VM = VehicleModel(CP)
    self.pt_cp = get_powertrain_can_parser(CP, canbus)
    self.ch_cp = get_chassis_can_parser(CP, canbus)
    self.ch_cp_dbc_name = DBC[CP.carFingerprint]['chassis']

    # sending if read only is False
    if sendcan is not None:
      self.sendcan = sendcan
      self.CC = CarController(canbus, CP.carFingerprint, CP.enableCamera)

  @staticmethod
  def compute_gb(accel, speed):
  	# Ripped from compute_gb_honda in Honda's interface.py. Works well off shelf but may need more tuning
    creep_brake = 0.0
    creep_speed = 2.68
    creep_brake_value = 0.10
    if speed < creep_speed:
      creep_brake = (creep_speed - speed) / creep_speed * creep_brake_value
    return float(accel) / 4.8 - creep_brake

  @staticmethod
  def calc_accel_override(a_ego, a_target, v_ego, v_target):

    # normalized max accel. Allowing max accel at low speed causes speed overshoots
    max_accel_bp = [10, 20]    # m/s
    max_accel_v = [0.85, 1.0] # unit of max accel
    max_accel = interp(v_ego, max_accel_bp, max_accel_v)

    # limit the pcm accel cmd if:
    # - v_ego exceeds v_target, or
    # - a_ego exceeds a_target and v_ego is close to v_target

    eA = a_ego - a_target
    valuesA = [1.0, 0.1]
    bpA = [0.3, 1.1]

    eV = v_ego - v_target
    valuesV = [1.0, 0.1]
    bpV = [0.0, 0.5]

    valuesRangeV = [1., 0.]
    bpRangeV = [-1., 0.]

    # only limit if v_ego is close to v_target
    speedLimiter = interp(eV, bpV, valuesV)
    accelLimiter = max(interp(eA, bpA, valuesA), interp(eV, bpRangeV, valuesRangeV))

    # accelOverride is more or less the max throttle allowed to pcm: usually set to a constant
    # unless aTargetMax is very high and then we scale with it; this help in quicker restart

    return float(max(max_accel, a_target / FOLLOW_AGGRESSION)) * min(speedLimiter, accelLimiter)

  @staticmethod
  def get_params(candidate, fingerprint):
    ret = car.CarParams.new_message()

    ret.carName = "gm"
    ret.carFingerprint = candidate

    ret.enableCruise = False
    ret.steerMPCReactTime = 0.025     # increase total MPC projected time by 25 ms
    ret.steerMPCDampTime = 0.15       # dampen desired angle over 250ms (5 mpc cycles)

    # Presence of a camera on the object bus is ok.
    # Have to go passive if ASCM is online (ACC-enabled cars),
    # or camera is on powertrain bus (LKA cars without ACC).
    ret.enableCamera = not any(x for x in STOCK_CONTROL_MSGS[candidate] if x in fingerprint)
    ret.openpilotLongitudinalControl = ret.enableCamera

    std_cargo = 136

    if candidate == CAR.VOLT:
      # supports stop and go, but initial engage must be above 18mph (which include conservatism)
      ret.minEnableSpeed = 8 * CV.MPH_TO_MS
      # kg of standard extra cargo to count for driver, gas, etc...
      ret.mass = 1607 + std_cargo
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.wheelbase = 2.69
      ret.steerRatio = 15.7
      ret.steerRatioRear = 0.
      ret.centerToFront = ret.wheelbase * 0.4 # wild guess

    elif candidate == CAR.MALIBU:
      # supports stop and go, but initial engage must be above 18mph (which include conservatism)
      ret.minEnableSpeed = 18 * CV.MPH_TO_MS
      ret.mass = 1496 + std_cargo
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.wheelbase = 2.83
      ret.steerRatio = 15.8
      ret.steerRatioRear = 0.
      ret.centerToFront = ret.wheelbase * 0.4 # wild guess

    elif candidate == CAR.HOLDEN_ASTRA:
      # kg of standard extra cargo to count for driver, gas, etc...
      ret.mass = 1363 + std_cargo
      ret.wheelbase = 2.662
      # Remaining parameters copied from Volt for now
      ret.centerToFront = ret.wheelbase * 0.4
      ret.minEnableSpeed = 18 * CV.MPH_TO_MS
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.steerRatio = 15.7
      ret.steerRatioRear = 0.

    elif candidate == CAR.ACADIA:
      ret.minEnableSpeed = -1 # engage speed is decided by pcm
      ret.mass = 4353. * CV.LB_TO_KG + std_cargo
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.wheelbase = 2.86
      ret.steerRatio = 14.4  #end to end is 13.46
      ret.steerRatioRear = 0.
      ret.centerToFront = ret.wheelbase * 0.4

    elif candidate == CAR.BUICK_REGAL:
      ret.minEnableSpeed = 18 * CV.MPH_TO_MS
      ret.mass = 3779. * CV.LB_TO_KG + std_cargo # (3849+3708)/2
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.wheelbase = 2.83 #111.4 inches in meters
      ret.steerRatio = 14.4 # guess for tourx
      ret.steerRatioRear = 0.
      ret.centerToFront = ret.wheelbase * 0.4 # guess for tourx

    elif candidate == CAR.CADILLAC_ATS:
      ret.minEnableSpeed = 18 * CV.MPH_TO_MS
      ret.mass = 1601 + std_cargo
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.wheelbase = 2.78
      ret.steerRatio = 15.3
      ret.steerRatioRear = 0.
      ret.centerToFront = ret.wheelbase * 0.49

    elif candidate == CAR.CADILLAC_CT6:
      # engage speed is decided by pcm
      ret.minEnableSpeed = -1
      # kg of standard extra cargo to count for driver, gas, etc...
      ret.mass = 4016. * CV.LB_TO_KG + std_cargo
      ret.safetyModel = car.CarParams.SafetyModels.cadillac
      ret.wheelbase = 3.11
      ret.steerRatio = 14.6   # it's 16.3 without rear active steering
      ret.steerRatioRear = 0. # TODO: there is RAS on this car!
      ret.centerToFront = ret.wheelbase * 0.465

    elif candidate == CAR.YUKON:
      ret.minEnableSpeed = 18 * CV.KPH_TO_MS # engage speed must be 18kph or over
      ret.mass = 5743 * CV.LB_TO_KG + std_cargo
      ret.safetyModel = car.CarParams.SafetyModels.gm
      ret.wheelbase = 2.946
      ret.steerRatio = 17.75  #end to end is 13.46
      ret.steerRatioRear = 0.
      ret.centerToFront = ret.wheelbase * 0.4

    # hardcoding honda civic 2016 touring params so they can be used to
    # scale unknown params for other cars
    mass_civic = 2923. * CV.LB_TO_KG + std_cargo
    wheelbase_civic = 2.70
    centerToFront_civic = wheelbase_civic * 0.4
    centerToRear_civic = wheelbase_civic - centerToFront_civic
    rotationalInertia_civic = 2500
    tireStiffnessFront_civic = 85400
    tireStiffnessRear_civic = 90000

    centerToRear = ret.wheelbase - ret.centerToFront
    # TODO: get actual value, for now starting with reasonable value for
    # civic and scaling by mass and wheelbase
    ret.rotationalInertia = rotationalInertia_civic * \
                            ret.mass * ret.wheelbase**2 / (mass_civic * wheelbase_civic**2)

    # TODO: start from empirically derived lateral slip stiffness for the civic and scale by
    # mass and CG position, so all cars will have approximately similar dyn behaviors
    ret.tireStiffnessFront = tireStiffnessFront_civic * \
                             ret.mass / mass_civic * \
                             (centerToRear / ret.wheelbase) / (centerToRear_civic / wheelbase_civic)
    ret.tireStiffnessRear = tireStiffnessRear_civic * \
                            ret.mass / mass_civic * \
                            (ret.centerToFront / ret.wheelbase) / (centerToFront_civic / wheelbase_civic)


    # same tuning for Volt and CT6 for now
    ret.steerKiBP, ret.steerKpBP = [[0.], [0.]]
    ret.steerKpV, ret.steerKiV = [[0.2], [0.0001]]
    ret.steerKf = 0.00004   # full torque for 20 deg at 80mph means 0.00007818594

    ret.steerMaxBP = [0.] # m/s
    ret.steerMaxV = [1.]
    ret.gasMaxBP = [0.]
    ret.gasMaxV = [0.5]
    ret.brakeMaxBP = [0.]
    ret.brakeMaxV = [1.]
    ret.longPidDeadzoneBP = [0.]
    ret.longPidDeadzoneV = [0.]

    ret.longitudinalKpBP = [0., 5., 35.]
    ret.longitudinalKpV = [3.3, 2.425, 2.2]
    ret.longitudinalKiBP = [0., 35.]
    ret.longitudinalKiV = [0.18, 0.36]

    ret.steerLimitAlert = True

    ret.stoppingControl = True
    ret.startAccel = 0.8

    ret.steerActuatorDelay = 0.1  # Default delay, not measured yet
    ret.steerRateCost = 1.0
    ret.steerControlType = car.CarParams.SteerControlType.torque
    ret.rateFFGain = 0.01

    return ret

  # returns a car.CarState
  def update(self, c):

    self.pt_cp.update(int(sec_since_boot() * 1e9), True)
    self.CS.update(self.pt_cp)

    # create message
    ret = car.CarState.new_message()

    # speeds
    ret.vEgo = self.CS.v_ego
    ret.aEgo = self.CS.a_ego
    ret.vEgoRaw = self.CS.v_ego_raw
    ret.yawRate = self.VM.yaw_rate(self.CS.angle_steers * CV.DEG_TO_RAD, self.CS.v_ego)
    ret.standstill = self.CS.standstill
    ret.wheelSpeeds.fl = self.CS.v_wheel_fl
    ret.wheelSpeeds.fr = self.CS.v_wheel_fr
    ret.wheelSpeeds.rl = self.CS.v_wheel_rl
    ret.wheelSpeeds.rr = self.CS.v_wheel_rr

    # gas pedal information.
    ret.gas = self.CS.pedal_gas / 254.0
    ret.gasPressed = self.CS.user_gas_pressed

    # brake pedal
    ret.brake = self.CS.user_brake / 0xd0
    ret.brakePressed = self.CS.brake_pressed
    ret.brakeLights = self.CS.frictionBrakesActive
    
    # steering wheel
    ret.steeringAngle = self.CS.angle_steers

    # torque and user override. Driver awareness
    # timer resets when the user uses the steering wheel.
    ret.steeringPressed = self.CS.steer_override
    ret.steeringTorque = self.CS.steer_torque_driver

    # cruise state
    ret.cruiseState.available = bool(self.CS.main_on)
    cruiseEnabled = self.CS.pcm_acc_status != 0
    ret.cruiseState.enabled = cruiseEnabled
    ret.cruiseState.standstill = False

    ret.leftBlinker = self.CS.left_blinker_on
    ret.rightBlinker = self.CS.right_blinker_on
    ret.doorOpen = not self.CS.door_all_closed
    ret.seatbeltUnlatched = not self.CS.seatbelt
    ret.gearShifter = self.CS.gear_shifter
    ret.readdistancelines = self.CS.follow_level

    buttonEvents = []

    # blinkers
    if self.CS.left_blinker_on != self.CS.prev_left_blinker_on:
      be = car.CarState.ButtonEvent.new_message()
      be.type = 'leftBlinker'
      be.pressed = self.CS.left_blinker_on
      buttonEvents.append(be)

    if self.CS.right_blinker_on != self.CS.prev_right_blinker_on:
      be = car.CarState.ButtonEvent.new_message()
      be.type = 'rightBlinker'
      be.pressed = self.CS.right_blinker_on
      buttonEvents.append(be)

    if self.CS.cruise_buttons != self.CS.prev_cruise_buttons:
      be = car.CarState.ButtonEvent.new_message()
      be.type = 'unknown'
      if self.CS.cruise_buttons != CruiseButtons.UNPRESS:
        be.pressed = True
        but = self.CS.cruise_buttons
      else:
        be.pressed = False
        but = self.CS.prev_cruise_buttons
      if but == CruiseButtons.RES_ACCEL:
        if not (cruiseEnabled and self.CS.standstill):
          be.type = 'accelCruise' # Suppress resume button if we're resuming from stop so we don't adjust speed.
      elif but == CruiseButtons.DECEL_SET:
        be.type = 'decelCruise'
        if not cruiseEnabled and not self.CS.lkMode:
          self.lkMode = True
      elif but == CruiseButtons.CANCEL:
        be.type = 'cancel'
      elif but == CruiseButtons.MAIN:
        be.type = 'altButton3'
      buttonEvents.append(be)

    ret.buttonEvents = buttonEvents
    
    if cruiseEnabled and self.CS.lka_button and self.CS.lka_button != self.CS.prev_lka_button:
      self.CS.lkMode = not self.CS.lkMode

    if self.CS.distance_button and self.CS.distance_button != self.CS.prev_distance_button:
       self.CS.follow_level -= 1
       if self.CS.follow_level < 1:
         self.CS.follow_level = 3

    events = []
    if not self.CS.can_valid:
      self.can_invalid_count += 1
      if self.can_invalid_count >= 5:
        events.append(create_event('commIssue', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE]))
    else:
      self.can_invalid_count = 0
    if cruiseEnabled and (self.CS.left_blinker_on or self.CS.right_blinker_on):
       events.append(create_event('manualSteeringRequiredBlinkersOn', [ET.WARNING]))
    if self.CS.steer_error:
      events.append(create_event('steerUnavailable', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE, ET.PERMANENT]))
    if self.CS.steer_not_allowed:
      events.append(create_event('steerTempUnavailable', [ET.NO_ENTRY, ET.WARNING]))
    if ret.doorOpen:
      events.append(create_event('doorOpen', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
    if ret.seatbeltUnlatched:
      events.append(create_event('seatbeltNotLatched', [ET.NO_ENTRY, ET.SOFT_DISABLE]))

    if self.CS.car_fingerprint in SUPERCRUISE_CARS:
      if self.CS.acc_active and not self.acc_active_prev:
        events.append(create_event('pcmEnable', [ET.ENABLE]))
      if not self.CS.acc_active:
        events.append(create_event('pcmDisable', [ET.USER_DISABLE]))

    else:
      if self.CS.brake_error:
        events.append(create_event('brakeUnavailable', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE, ET.PERMANENT]))
      if not self.CS.gear_shifter_valid:
        events.append(create_event('wrongGear', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
      if self.CS.esp_disabled:
        events.append(create_event('espDisabled', [ET.NO_ENTRY, ET.SOFT_DISABLE]))
      if not self.CS.main_on:
        events.append(create_event('wrongCarMode', [ET.NO_ENTRY, ET.USER_DISABLE]))
      if self.CS.gear_shifter == 3:
        events.append(create_event('reverseGear', [ET.NO_ENTRY, ET.IMMEDIATE_DISABLE]))
      if ret.vEgo < self.CP.minEnableSpeed:
        events.append(create_event('speedTooLow', [ET.NO_ENTRY]))
      if self.CS.park_brake:
        events.append(create_event('parkBrake', [ET.NO_ENTRY, ET.USER_DISABLE]))
      # disable on pedals rising edge or when brake is pressed and speed isn't zero
      if (ret.gasPressed and not self.gas_pressed_prev) or \
        (ret.brakePressed): # and (not self.brake_pressed_prev or ret.vEgo > 0.001)):
        events.append(create_event('pedalPressed', [ET.NO_ENTRY, ET.USER_DISABLE]))
      if ret.gasPressed:
        events.append(create_event('pedalPressed', [ET.PRE_ENABLE]))

      # handle button presses
      for b in ret.buttonEvents:
        # do enable on both accel and decel buttons
        if b.type in ["accelCruise", "decelCruise"] and not b.pressed:
          events.append(create_event('buttonEnable', [ET.ENABLE]))
        # do disable on button down
        if b.type == "cancel" and b.pressed:
          events.append(create_event('buttonCancel', [ET.USER_DISABLE]))

    ret.events = events

    # update previous brake/gas pressed
    self.acc_active_prev = self.CS.acc_active
    self.gas_pressed_prev = ret.gasPressed
    self.brake_pressed_prev = ret.brakePressed

    # cast to reader so it can't be modified
    return ret.as_reader()

  # pass in a car.CarControl
  # to be called @ 100hz
  def apply(self, c):
    hud_v_cruise = c.hudControl.setSpeed
    if hud_v_cruise > 70:
      hud_v_cruise = 0

    chime, chime_count = AUDIO_HUD[c.hudControl.audibleAlert.raw]

    # For Openpilot, "enabled" includes pre-enable.
    # In GM, PCM faults out if ACC command overlaps user gas.
    enabled = c.enabled and not self.CS.user_gas_pressed

    self.CC.update(self.sendcan, enabled, self.CS, self.frame, \
      c.actuators,
      hud_v_cruise, c.hudControl.lanesVisible, \
      c.hudControl.leadVisible, \
      chime, chime_count)

    self.frame += 1
