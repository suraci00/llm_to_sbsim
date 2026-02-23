from smart_control.proto import smart_control_building_pb2
import numpy as np
def dump_devices_and_setpoints(env): #per mappare i device e i rispettivi setpoints
    building = env.building

    print("\n=== ZONES ===")
    for z in building.zones:
        print(f"- {z.zone_id}: devices={list(z.devices)}")

    print("\n=== DEVICES (controllabili/leggibili) ===")
    for d in building.devices:
        action_setpoints = sorted(list(d.action_fields.keys()))      # setpoint controllabili
        observable_meas = sorted(list(d.observable_fields.keys()))   # measurement leggibili

        print(f"\nDevice: {d.device_id} | zone: {d.zone_id} | type: {d.device_type}")
        print(f"  controllabili (setpoints): {action_setpoints}")
        print(f"  leggibili (measurements):  {observable_meas}")


def sanity_check_building(env): #verifica associazione tra device e zone (1 device per zona)
    building = env.building
    zone_ids = [z.zone_id for z in building.zones]
    device_ids = [d.device_id for d in building.devices]

    print("\n=== SANITY CHECK ===")
    print("zones:", len(zone_ids))
    print("devices:", len(device_ids))

    # tutte le zone hanno esattamente 1 device?
    bad = [z for z in building.zones if len(z.devices) != 1]
    print("zones with !=1 device:", len(bad))

    # ogni device associato a una zona esiste davvero come device_id?
    zone_devices = {dev for z in building.zones for dev in z.devices}
    missing = sorted(list(zone_devices - set(device_ids)))
    print("zone->device missing in building.devices:", len(missing))
    if missing[:5]:
        print("example missing:", missing[:5])

    # verifica runtime RewardInfo (una volta che env è resettato/avviato)
    ri = building.reward_info
    ri_zone_ids = set(ri.zone_reward_infos.keys())
    b_zone_ids = set(zone_ids)
    print("RewardInfo zones:", len(ri_zone_ids))
    print("zones in building but not in RewardInfo:", len(b_zone_ids - ri_zone_ids))
    print("zones in RewardInfo but not in building:", len(ri_zone_ids - b_zone_ids))


def dump_action_mapping(env): #mappa il numero di azioni disponibili
    spec = env.action_spec()
    print("\n=== ACTION SPEC ===")
    print("shape:", spec.shape, "dtype:", spec.dtype)
    if hasattr(spec, "minimum"):
        print("min:", spec.minimum)
    if hasattr(spec, "maximum"):
        print("max:", spec.maximum)

    n = int(np.prod(spec.shape))

    print("\n=== CANDIDATE ACTION NAME LISTS (len==num_actions) ===")
    for attr in dir(env):
        if attr.startswith("_"):
            continue
        try:
            val = getattr(env, attr)
        except Exception:
            continue

        if isinstance(val, (list, tuple)) and len(val) == n and all(isinstance(x, str) for x in val):
            print(f"- env.{attr} (len={len(val)})")
            print("  first 10:", val[:10])


def dump_real_action_names(env): #individua quali sono le azioni
    print("\n=== ACTION SPEC (raw) ===")
    print(env.action_spec())

    # Prova 1: attributi interni (spesso esistono)
    for attr in ["_action_names", "action_names", "_action_normalizers", "_action_spec"]:
        if hasattr(env, attr):
            val = getattr(env, attr)
            print(f"\n=== env.{attr} ===")
            print(val)

    # Prova 2: se action_names è un dict/struttura annidata, stampa chiavi + primi elementi
    if hasattr(env, "_action_names"):
        an = env._action_names
        try:
            # se è un dict
            if isinstance(an, dict):
                print("\n_action_names keys:", list(an.keys())[:20])
        except Exception:
            pass

    # Prova 3: stampa “piatto” (se è annidato)
    try:
        import tensorflow as tf
        flat_spec = tf.nest.flatten(env.action_spec())
        print("\n=== FLATTENED action_spec ===")
        for i, s in enumerate(flat_spec):
            # s può essere BoundedArraySpec; stampo shape/min/max
            mn = getattr(s, "minimum", None)
            mx = getattr(s, "maximum", None)
            print(f"[{i}] shape={s.shape} min={mn} max={mx}")
    except Exception as e:
        print("\n(flatten skipped):", e)


def test_setpoint_vav(env): #test accettazione setpoint su vav

    env.reset()
    building = env.building
    vav = next(d for d in building.devices if d.device_id == "vav_room_1") #prendo il nome live perchè può cambiare quando faccio env.reset()
    print("VAV:", vav.device_id, "setpoints:", list(vav.action_fields.keys()))

    req = smart_control_building_pb2.ActionRequest()
    req.single_action_requests.append(
        smart_control_building_pb2.SingleActionRequest(
            device_id=vav.device_id,
            setpoint_name="supply_air_damper_percentage_command",
            continuous_value=0.5,
        )
    )

    resp = building.request_action(req)
    print("resp_type:", resp.single_action_responses[0].response_type)

    obs = smart_control_building_pb2.ObservationRequest()
    obs.single_observation_requests.append(
        smart_control_building_pb2.SingleObservationRequest(
            device_id="vav_room_1",
            measurement_name="supply_air_damper_percentage_command",
        )
    )
    obs_resp = building.request_observations(obs)
    print("LETTO damper:", obs_resp.single_observation_responses[0].continuous_value)


def test_setpoint_boiler(env): #test accettazione setpoint su boiler

    env.reset()
    building = env.building
    boiler = next(d for d in building.devices if "boiler" in d.device_id) #prendo il nome live perchè può cambiare quando faccio env.reset()
    print("BOILER:", boiler.device_id, "setpoints:", list(boiler.action_fields.keys()))

    req = smart_control_building_pb2.ActionRequest()
    req.single_action_requests.append(
        smart_control_building_pb2.SingleActionRequest(
            device_id=boiler.device_id,
            setpoint_name="supply_water_setpoint",
            continuous_value=330.0,  # valore test
        )
    )

    resp = building.request_action(req)
    rt = resp.single_action_responses[0].response_type
    print("response_type:", smart_control_building_pb2.SingleActionResponse.ActionResponseType.Name(rt))
    print("info:", resp.single_action_responses[0].additional_info)

    obs = smart_control_building_pb2.ObservationRequest()
    obs.single_observation_requests.append(
        smart_control_building_pb2.SingleObservationRequest(
            device_id=boiler.device_id,
            measurement_name="supply_water_setpoint",
        )
    )
    obs_resp = building.request_observations(obs)
    print("LETTO supply_water_setpoint:", obs_resp.single_observation_responses[0].continuous_value)
