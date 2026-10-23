"""Electric drive: a motor, a battery and one fixed reduction instead of an engine, a gearbox and a fuel tank.

Laid out like BeamNG's own electric parts: an electricMotor device that the
vehicleController's "electricMotor" shift logic runs forwards and backwards
(so there is no gearbox), fed by an electricBattery. The motor turns the
driveshafts, or a centre differential, straight away.

A motor gives its full torque from standstill up to its base speed and the
same power above it, fading a little towards its top speed.
"""
import math

from .shape import clamp

DEVICE = "mainMotor"
BATTERY = "mainBattery"
SOUND = "ElectricMotor_02"
MAX_RPM = 12000          # the motor's top speed
BASE = 0.32              # full torque up to this share of the top speed
KG_PER_KWH = 6.0         # a battery pack's weight for each kWh it holds
EFFICIENCY = 0.92        # electrical energy that becomes torque


def motor(power_hp):
    """The motor's torque curve and numbers, like Generator.engine has them for an engine."""
    p_w = power_hp * 745.7
    base = MAX_RPM * BASE
    t0 = p_w / (base * 2 * math.pi / 60)
    curve = []
    for f in (0.0, 0.08, 0.16, 0.24, BASE, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        rpm = f * MAX_RPM
        if rpm <= base:
            t = t0
        else:
            t = p_w * (1.0 - 0.12 * (rpm - base) / (MAX_RPM - base)) / (rpm * 2 * math.pi / 60)
        curve.append((rpm, t))
    return {"curve": curve, "peak_torque": t0, "idle": 0, "max_rpm": MAX_RPM, "limit": MAX_RPM,
            "inertia": clamp(0.04 + power_hp / 5000.0, 0.05, 0.3), "peak_power_rpm": base, "peak_torque_rpm": 0}


def motor_mass(power_hp):
    return clamp(40 + 0.16 * power_hp, 45, 260)


def reduction(max_rpm, r, top_speed_kmh):
    """Motor turns per wheel turn so the motor reaches its top speed at the vehicle's top speed."""
    v = max(top_speed_kmh, 20) / 3.6
    return clamp((max_rpm * 0.98 * 2 * math.pi / 60) * r / v, 3.0, 16.0)


def battery_mass(kwh):
    return KG_PER_KWH * kwh


def range_km(kwh, mass, cda, crr, speed_kmh=100.0):
    """How far a full battery goes at a steady speed on level ground."""
    v = speed_kmh / 3.6
    force = 0.5 * 1.2 * cda * v * v + crr * mass * 9.81
    wh_per_km = force * 1000.0 / 3600.0 / (EFFICIENCY * 0.92)
    return kwh * 1000.0 / max(wh_per_km, 1.0)


def motor_section(e, reaction_nodes):
    """The electricMotor device's settings."""
    return {
        "torque": [["rpm", "torque"]] + [[int(round(r)), round(t, 1)] for r, t in e["curve"]],
        "maxRPM": int(e["max_rpm"]), "inertia": round(e["inertia"], 3), "friction": 0, "dynamicFriction": 0,
        "electricalEfficiency": EFFICIENCY, "maximumWantedRegenTorque": int(round(e["peak_torque"])),
        "torqueReactionNodes:": list(reaction_nodes), "energyStorage": [BATTERY],
        "uiName": "Electric Motor", "soundConfig": "soundConfig",
    }


def controller():
    """The vehicleController settings that make the motor drive (forwards, backwards, regenerative braking)."""
    return {"shiftLogicName": "electricMotor", "motorNames": [DEVICE], "topSpeedLimitReverse": -1,
            "defaultRegen": 0.5, "brakeRegenCoef": 1}


def sound(power_hp):
    return {"sampleName": SOUND, "mainGain": round(clamp(-12 + 2.0 * math.log10(max(power_hp, 30) / 30.0) * 2, -12, -4), 1)}


def battery_sections(kwh):
    """energyStorage and the battery itself; the charge at the start is the $fuel variable."""
    return {
        "energyStorage": [["type", "name"], ["electricBattery", BATTERY]],
        BATTERY: {"energyType": "electricEnergy", "batteryCapacity": kwh, "startingCapacity": "$fuel"},
    }
