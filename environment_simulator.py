#--FUNZIONI PER TEST DI COMPARAZIONE TRA 2 VALORI DIVERSI DELLO STESSO SETPOINT
def read_point_for_debug(building, device_id: str, measurement_name: str):
    req = smart_control_building_pb2.ObservationRequest()
    req.single_observation_requests.append(
        smart_control_building_pb2.SingleObservationRequest(
            device_id=device_id,
            measurement_name=measurement_name,
        )
    )
    try:
        resp = building.request_observations(req)
        return float(resp.single_observation_responses[0].continuous_value)
    except Exception:
        return float("nan")

def quick_two_value_override_test(
    gin_file: str,
    device_prefix: str,
    setpoint_name: str,
    value_a: float,
    value_b: float,
    n_steps: int = 96,
):
    def run_case(label: str, value: float):
        env = load_environment(gin_file)
        env.reset()
        device_id = find_single_device_by_prefix(env.building, device_prefix)
        print(f"[{label}] device risolto: {device_id}")
        action_spec = env.action_spec()
        current_action = np.zeros(action_spec.shape, dtype=action_spec.dtype)

        rows = []
        overrides = {
            (device_id, setpoint_name): value
        }

        for i in range(n_steps):
            apply_user_overrides(env.building, overrides)

            ts = env.step(current_action)

            reward_info = env.building.reward_info
            reward_response = env.reward_function.compute_reward(reward_info)

            rows.append({
                "case": label,
                "step": i,
                "timestamp": str(env.current_simulation_timestamp),
                "setpoint_value": value,
                "readback_setpoint": read_point_for_debug(
                    env.building,
                    device_id,
                    setpoint_name,
                ),
                "supply_air_flowrate_sensor": read_point_for_debug(
                    env.building,
                    device_id,
                    "supply_air_flowrate_sensor",
                ),
                "outside_air_flowrate_sensor": read_point_for_debug(
                    env.building,
                    device_id,
                    "outside_air_flowrate_sensor",
                ),
                "reward": safe_float(ts.reward),
                "electricity_cost": safe_float(reward_response.electricity_energy_cost),
                "gas_cost": safe_float(reward_response.natural_gas_energy_cost),
                "carbon": safe_float(reward_response.carbon_emitted),
                "electricity_kwh": electricity_kwh_step(reward_info, 300),
                "gas_kwh": gas_kwh_step(reward_info, 300),
                "avg_temp_c": building_avg_temp_c_step(reward_info),
            })

        return pd.DataFrame(rows)

    df_a = run_case("A", value_a)
    df_b = run_case("B", value_b)

    comparison = df_a.merge(
        df_b,
        on="step",
        suffixes=("_A", "_B")
    )

    for col in [
        "reward",
        "electricity_cost",
        "gas_cost",
        "carbon",
        "electricity_kwh",
        "gas_kwh",
        "avg_temp_c",
        "readback_setpoint",
        "supply_air_flowrate_sensor",
        "outside_air_flowrate_sensor",
    ]:
        comparison[f"delta_{col}"] = comparison[f"{col}_B"] - comparison[f"{col}_A"]

    os.makedirs("logs", exist_ok=True)
    df_a.to_csv("logs/cooling_setpoint_A.csv", index=False)
    df_b.to_csv("logs/cooling_setpoint_B.csv", index=False)
    comparison.to_csv("logs/cooling_setpoint_comparison.csv", index=False)

    print("\n=== TWO VALUE OVERRIDE TEST ===")
    print(f"Device: {device_prefix}")
    print(f"Setpoint: {setpoint_name}")
    print(f"A = {value_a}")
    print(f"B = {value_b}")

    print("\nDelta aggregati:")
    for col in [
        "reward",
        "electricity_cost",
        "gas_cost",
        "carbon",
        "electricity_kwh",
        "gas_kwh",
        "avg_temp_c",
        "readback_setpoint",
        "supply_air_flowrate_sensor",
        "outside_air_flowrate_sensor",
    ]:
        delta_abs_sum = comparison[f"delta_{col}"].abs().sum()
        print(f"{col}: abs_delta_sum = {delta_abs_sum}")

    print("\nCSV generati:")
    print("logs/cooling_setpoint_A.csv")
    print("logs/cooling_setpoint_B.csv")
    print("logs/cooling_setpoint_comparison.csv")

    return comparison

def find_single_device_by_prefix(building, prefix: str) -> str:
    matches = [
        dev.device_id
        for dev in building.devices
        if dev.device_id.startswith(prefix)
    ]

    if len(matches) != 1:
        raise ValueError(
            f"Atteso 1 device con prefisso '{prefix}', trovati {len(matches)}: {matches}"
        )

    return matches[0]
#FINE FUNZIONI DI TEST DI COMPARAZIONE



import os
import csv
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Literal
import gin
import numpy as np
import pandas as pd
from pydantic import BaseModel, ValidationError
import json
import ollama
import time
from openpyxl import Workbook
import prompts
import test_utils as test

from smart_control.environment import environment as sc_environment
from smart_control.proto import smart_control_building_pb2

# gli import seguenti NON servono "per usarli nel codice", ma per farli conoscere a Gin (altrimenti non funzionava)
from smart_control.utils import controller_reader  # registra controller_reader.ProtoReader
from smart_control.reinforcement_learning.utils.config import get_histogram_path  # registra @get_histogram_path()

'''INFO BASE
AHU = centrale aria edificio
VAV = valvola/serranda di regolazione per singola stanza
Boiler = centrale acqua calda
Setpoint controllabili da Agente RL: temperatura acqua calda boiler, temperatura aria calda AHU
Gli altri setpoint sono impostati dall'edificio tramite comandi dell'utente
'''

def load_environment(gin_config_file: str):
    gin.clear_config() #per pulire configurazione gin nel caso vengano eseguite più run
    """
    1) Importa i "configurable" usati nel file .gin (ProtoReader e get_histogram_path)
       così Gin li riconosce.
    2) Fa parse del file .gin, che contiene tutte le impostazioni dell'ambiente.
    3) Crea e restituisce l'Environment configurato.
    """
    # se il path non è assoluto, lo rendo relativo alla cartella attuale (opzionale ma per sicurezza)
    gin_config_file = os.path.abspath(gin_config_file)
    # sblocca gin per permettere il parsing
    with gin.unlock_config():
        # legge il file gin e registra tutti i parametri
        gin.parse_config_file(gin_config_file)
    # crea l'Environment usando ciò che gin ha configurato
    env = sc_environment.Environment()
    return env


@dataclass #per creare un 'oggetto log'
class StepLog: #classe 'singolo passo temporale'
    step_idx: int
    timestamp: str
    reward: float
    electricity_cost: float
    gas_cost: float
    carbon: float
    electricity_kwh: float
    gas_kwh: float
    avg_temp_c: float
    occupancy: float
    productivity_regret: float
    active_overrides_json: str = ""
    applied_writes_json: str = ""


class Intent(BaseModel):
    type: Literal["command", "query"]
    zone_id: Optional[str] = None
    device_id: Optional[str] = None
    measurement_name: Optional[str] = None
    setpoint_name: Optional[str] = None
    value: Optional[float] = None

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_devices",
            "description": "Restituisce zone, device, campi osservabili e campi controllabili del simulatore.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_point",
            "description": "Legge un measurement o un setpoint leggibile da un device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "measurement_name": {"type": "string"}
                },
                "required": ["device_id", "measurement_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_point",
            "description": "Imposta un setpoint controllabile su un device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_id": {"type": "string"},
                    "setpoint_name": {"type": "string"},
                    "value": {"type": "number"}
                },
                "required": ["device_id", "setpoint_name", "value"]
            }
        }
    }
]


def build_zone_map(building) -> dict:
    zone_map = {}
    for z in building.zones: #ritorno la lista delle zone e dei rispettivi device
        zone_map[z.zone_id] = list(z.devices)

    for dev in building.devices: #per individuare zone di device non trovate (es. default_zone)
        if dev.zone_id not in zone_map:
            zone_map[dev.zone_id] = []
        if dev.device_id not in zone_map[dev.zone_id]:
            zone_map[dev.zone_id].append(dev.device_id)

    return zone_map


def build_zone_details(building) -> dict: # per esportare le zone
    zone_details = {}
    for dev in building.devices:
        zone_id = dev.zone_id
        if zone_id not in zone_details:
            zone_details[zone_id] = []

        zone_details[zone_id].append({
            "device_id": dev.device_id,
            "device_type": dev.device_type,
            "setpoints": sorted(list(dev.action_fields.keys())),
            "measurements": sorted(list(dev.observable_fields.keys()))
        })
    return zone_details


def export_building_map_to_excel(building, output_path: str): # usato 1 volta per mappare tutto il building
    zone_details = build_zone_details(building) #importo zone
    #creo file e intestazione
    wb = Workbook()
    ws = wb.active
    ws.title = "building_map"
    ws.append(["zone_id", "device_id", "device_type", "setpoints", "measurements"])

    for zone_id in sorted(zone_details.keys()):
        devices = sorted(zone_details[zone_id], key=lambda x: x["device_id"])
        for dev in devices:
            ws.append([
                zone_id,
                dev["device_id"],
                dev["device_type"],
                ", ".join(dev["setpoints"]),
                ", ".join(dev["measurements"]),
            ])
    wb.save(output_path)


def view_env(building) -> dict:
    # ritorno per ogni device la lista di sensori e attuatori
    devices = {}
    for dev in building.devices:
        device_id = dev.device_id
        setpoints = list(dev.action_fields.keys())
        measurements = list(dev.observable_fields.keys())
        devices[device_id] = {
            "device_type": str(dev.device_type),
            "zone_id": dev.zone_id,
            "setpoints": setpoints,
            "measurements": measurements
        }
    return devices


def clean_tool_args(args: dict) -> dict:
    cleaned = {}
    for k, v in (args or {}).items():
        if v == "":
            cleaned[k] = None
        elif k == "value" and isinstance(v, str):
            try:
                cleaned[k] = float(v)
            except ValueError:
                cleaned[k] = v
        else:
            cleaned[k] = v
    return cleaned


def resolve_device_from_context(device_id, point_name, devices):
    #Risolve riferimenti generici come 'boiler' o 'ahu' verso i device_id reali
    if device_id in devices: # se ha già trovato il device -> OK
        return device_id

    raw_device = str(device_id or "").strip().lower()
    point_name = point_name or ""

    if point_name: #cerca i devices con il setpoint/measurement individuato
        candidates = [
            d for d, info in devices.items()
            if point_name in info.get("measurements", [])
            or point_name in info.get("setpoints", [])
        ]
        if len(candidates) == 1:
            return candidates[0]

    alias_prefixes = [] #mappatura sinonimi
    if any(x in raw_device for x in ["boiler", "caldaia"]):
        alias_prefixes = ["boiler"]
    elif any(x in raw_device for x in ["ahu", "air handler", "centrale aria", "air_handler"]):
        alias_prefixes = ["air_handler"]
    elif "vav" in raw_device:
        alias_prefixes = ["vav"]

    if alias_prefixes:  #per evitare che non riconosca i device che hanno un suffisso
        candidates = [d for d in devices if any(d.lower().startswith(prefix) for prefix in alias_prefixes)]
        if point_name:
            candidates = [
                d for d in candidates
                if point_name in devices[d].get("measurements", [])
                or point_name in devices[d].get("setpoints", [])
            ]
        if len(candidates) == 1:
            return candidates[0]

    if raw_device: #per evitare che non riconosca device scritti in maiuscolo/camel case/ecc.
        candidates = [d for d in devices if raw_device in d.lower()]
        if point_name:
            candidates = [
                d for d in candidates
                if point_name in devices[d].get("measurements", [])
                or point_name in devices[d].get("setpoints", [])
            ]
        if len(candidates) == 1:
            return candidates[0]

    return device_id


def get_local_override_value(building, device_id: str, setpoint_name: str) -> Optional[float]:
    #Restituisce il valore override salvato localmente per un setpoint utente
    overrides = getattr(building, "_user_runtime_overrides", {}) or {}
    key = (device_id, setpoint_name)
    if key in overrides:
        try:
            return float(overrides[key])
        except Exception:
            return overrides[key]
    return None


def esegui_tools(building, devices, zone_map, tool_name: str, args: dict) -> dict:
    #esegue le funzioni del tool richiamato dal function calling
    args = clean_tool_args(args)

    if tool_name == "list_devices":
        return {
            "zones": zone_map,
            "devices": devices,
        }

    point_name = args.get("measurement_name") or args.get("setpoint_name")
    device_id = resolve_device_from_context(args.get("device_id"), point_name, devices)

    if tool_name == "read_point":
        intent = Intent(
            type="query",
            device_id=device_id,
            measurement_name=point_name,
        )
        result = esegui_intent(building, intent, devices, zone_map)
        if isinstance(result, dict) and "error" not in result:
            result["success"] = True
        else:
            result["success"] = False
        return result

    if tool_name == "write_point":
        intent = Intent(
            type="command",
            device_id=device_id,
            setpoint_name=point_name,
            value=args.get("value"),
        )
        result = esegui_intent(building, intent, devices, zone_map)
        if isinstance(result, dict) and "error" not in result:
            result["success"] = result.get("response_type") == 1
        return result

    return {"error": f"Tool sconosciuto: {tool_name}"}


def build_system_prompt() -> str:
    base_prompt = prompts.prompt_funcalling_lowlevelwithcontext()
    extra_rules = """

    Regole aggiuntive per SBSim e function calling:
    - Non inventare mai device_id completi. Se conosci solo il tipo, usa nomi generici come boiler, caldaia, ahu, air handler o vav: il backend li risolverà.
    - Non usare default_zone_id come device_id: le zone non sono device.
    - Per leggere un setpoint usa read_point con measurement_name uguale al nome del setpoint.
    - Per modificare un setpoint usa write_point.
    - Se l'utente dice supply water setpoint del boiler, usa device_id boiler e measurement_name supply_water_setpoint.
    - Se l'utente dice damper o serranda di un VAV, usa supply_air_damper_percentage_command.
    """
    return base_prompt + extra_rules


def interpreta_prompt(user_text: str, system_prompt: str, building, devices, zone_map):
    print("DEBUG: sto chiamando Qwen...")
    print("DEBUG: testo utente =", user_text)
    t0 = time.time()

    resp = ollama.chat(
        model="qwen2.5:7b",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        tools=TOOLS,
    )

    dt = time.time() - t0
    print(f"DEBUG: Qwen ha risposto in {dt:.2f} secondi")
    message = resp["message"]
    print("DEBUG LLM MESSAGE:", message)
    tool_calls = message.get("tool_calls", [])
    if not tool_calls:
        return {
            "final_text": message.get("content", ""),
            "tool_results": [],
            "writes_applied": [],
        }

    results = []
    writes_applied = []
    for tc in tool_calls:
        fn = tc["function"]["name"]
        args = tc["function"]["arguments"]
        result = esegui_tools(building, devices, zone_map, fn, args)

        if fn == "write_point" and isinstance(result, dict) and result.get("response_type") == 1:
            result["success"] = True
            writes_applied.append(result)
        elif fn == "write_point" and isinstance(result, dict):
            result["success"] = False

        results.append({
            "tool_name": fn,
            "arguments": args,
            "result": result,
        })
    return {
        "final_text": None,
        "tool_results": results,
        "writes_applied": writes_applied,
    }


def esegui_intent(building, intent: Intent, devices: dict, zone_map: dict) -> dict:

    # CONTROLLI DI COERENZA

    # 1. Se c'è zone_id ma manca device_id, deduci device_id
    if intent.zone_id and not intent.device_id:
        devices_in_zone = zone_map.get(intent.zone_id)

        # 1.1 Se la zona indicata dall'utente non esiste
        if not devices_in_zone:
            return {
                "error": f"Zona '{intent.zone_id}' non valida.",
                "valid_zones": list(zone_map.keys()),
            }

        # Scegli un device della zona coerente col tipo di richiesta
        chosen = None
        if intent.type == "command":
            for d in devices_in_zone:
                if d in devices and len(devices[d]["setpoints"]) > 0:
                    chosen = d
                    break
        else: # query (si possono fare sia su setpoints che su measurements)
            for d in devices_in_zone:
                if d in devices and (
                    len(devices[d]["measurements"]) > 0 or
                    len(devices[d]["setpoints"]) > 0
                ):
                    chosen = d
                    break

        # errore se non trova devices coerenti con la zona richiesta
        if not chosen:
            return {
                "error": f"Nessun device utilizzabile trovato in {intent.zone_id} per {intent.type}.",
                "devices_in_zone": devices_in_zone,
            }

        intent.device_id = chosen

    # 2. Restituisci errore se device_id non è deducibile o è inesistente
    if not intent.device_id: #2.1 device_id non deducibile
        return {
            "error": "Manca device_id (e non è stata fornita una zone_id valida per dedurlo).",
            "suggerimento": "Specifica device_id oppure zona (zone_1/zone_2).",
        }

    if intent.device_id not in devices: #2.2 device_id inesistente
        return {
            "error": f"Device '{intent.device_id}' non esiste.",
            "valid_devices": list(devices.keys()),
            "zones": zone_map, # utile per capire dove sono i device
        }

    # 3. Se c'è zone_id, verifica coerenza device-zona
    if intent.zone_id:
        devices_in_zone = zone_map.get(intent.zone_id, [])
        if intent.device_id not in devices_in_zone:
            return {
                "error": f"Device '{intent.device_id}' non appartiene a {intent.zone_id}.",
                "devices_in_zone": devices_in_zone,
            }
    # --FINE CONTROLLI

    # QUERY
    if intent.type == "query":
        if not intent.measurement_name and intent.setpoint_name: #se l'utente dice 'dimmi setpoint 1', restituisco lo stato del setpoint 1 come fosse una misurazione
            intent.measurement_name = intent.setpoint_name

        if not intent.measurement_name: #errore se measurement_name = none
            return {
                "error": "Query senza measurement_name.",
                "valid_measurements": devices[intent.device_id]["measurements"], # li leggo come measurement_name
            }

        # verifico se il campo esiste sul device specifico
        valid_measurements = set(devices[intent.device_id]["measurements"])
        valid_readable_setpoints = set(devices[intent.device_id]["setpoints"])

        if (
            intent.measurement_name not in valid_measurements and
            intent.measurement_name not in valid_readable_setpoints
        ):
            return {
                "error": f"Campo '{intent.measurement_name}' non valido per {intent.device_id}.",
                "valid_measurements": sorted(valid_measurements),
            }

        # creo un'istanza specifica per la richiesta dell'utente
        req = smart_control_building_pb2.ObservationRequest()
        req.single_observation_requests.append(
            smart_control_building_pb2.SingleObservationRequest(
                device_id=intent.device_id,
                measurement_name=intent.measurement_name,
            )
        )

        # restituisco la risposta
        resp = building.request_observations(req)
        v = resp.single_observation_responses[0].continuous_value #0 perchè al momento processiamo una richiesta alla volta quindi prendiamo il primo elemento
        return {
            "type": "query",
            "zone_id": intent.zone_id,
            "device_id": intent.device_id,
            "measurement_name": intent.measurement_name,
            "value": float(v),
        }

    # COMMAND
    if not intent.setpoint_name or intent.value is None: #errore se setpoint_name o valore = none
        return {
            "error": "Command senza setpoint_name o value.",
            "valid_setpoints": devices[intent.device_id]["setpoints"],
        }

    # validazione setpoints (controllo che il setpoint sia disponibile all'interno dell'intent_device)
    valid_setpoints = set(devices[intent.device_id]["setpoints"])
    if intent.setpoint_name not in valid_setpoints:
        return {
            "error": f"Setpoint '{intent.setpoint_name}' non valido per {intent.device_id}.",
            "valid_setpoints": sorted(valid_setpoints),
        }

    # creo un'istanza specifica per la richiesta dell'utente
    req = smart_control_building_pb2.ActionRequest()
    req.single_action_requests.append(
        smart_control_building_pb2.SingleActionRequest(
            device_id=intent.device_id,
            setpoint_name=intent.setpoint_name,
            continuous_value=float(intent.value),
        )
    )

    # restituisco la risposta
    resp = building.request_action(req)
    # 1 = ACCEPTED
    response_type = int(resp.single_action_responses[0].response_type) #0 perchè al momento processiamo una richiesta alla volta quindi prendiamo il primo elemento
    return {
        "type": "command",
        "zone_id": intent.zone_id,
        "device_id": intent.device_id,
        "setpoint_name": intent.setpoint_name,
        "requested_value": float(intent.value),
        "response_type": response_type,
    }


def read_back_point(building, device_id: str, point_name: str) -> Optional[float]:
    # Chiede al simulatore di leggere un valore in input
    req = smart_control_building_pb2.ObservationRequest()
    # Aggiungo dentro la richiesta:
    # "vai su questo device e leggimi questo campo"
    req.single_observation_requests.append(
        smart_control_building_pb2.SingleObservationRequest(
            device_id=device_id,          # nome del dispositivo (es: vav_room_1)
            measurement_name=point_name,  # nome del valore da leggere
        )
    )
    try:
        # Invio la richiesta al simulatore
        resp = building.request_observations(req)
        # Prendo il risultato e lo trasformo in numero
        return float(resp.single_observation_responses[0].continuous_value)
    except Exception:
        # Se qualcosa va storto (device non esiste, campo non esiste, ecc.)
        return None


def get_action_mapping(env) -> dict:
    # Lista interna delle azioni dell'environment. Se non esiste, usa lista vuota
    mapping = {}
    # attributi dell'Environment
    action_names = getattr(env, "_action_names", []) #nomi actions
    id_map = getattr(env, "_id_map", None) #mappa che collega le azioni ai nomi dei device
    normalizers = getattr(env, "action_normalizers", {}) #normalizzatori azione
    if id_map is None:
        return mapping
    for i, field_id in enumerate(action_names): # Scorro tutte le azioni disponibili
        try:
            device_id, setpoint_name = id_map.inv[field_id]  # Recupero device_id e nome del setpoint da field_id
        except Exception:
            continue
        # Salvo tutto nel dizionario
        mapping[(str(device_id), str(setpoint_name))] = {
            "index": i,
            "field_id": field_id,
            "normalizer": normalizers.get(field_id),
        }
    return mapping


def make_initial_action(env) -> np.ndarray:
    """uso eventuali valori di default previsti dall'ambiente
    converto questi valori in un array numpy
    questa cosa serve per far partire la simulazione con i valori di default e non con array di zero"""
    action_spec = env.action_spec() # Prendo le informazioni sull'action space (dimensioni, tipo, ecc.)
    dtype = getattr(action_spec, "dtype", np.float32) # prova a prendere dtype, se non c'è usa float32
    shape = getattr(action_spec, "shape", ())
    default_values = getattr(env, "default_policy_values", None) # Provo a prendere valori iniziali già pronti

    # Se è un oggetto TensorFlow lo trasformo in numpy
    if default_values is not None:
        try:
            arr = default_values.numpy()
        except Exception:
            try:
                arr = np.asarray(default_values)
            except Exception:
                arr = None
        # Se la forma è giusta, uso questi valori
        if arr is not None and tuple(arr.shape) == tuple(shape):
            return arr.astype(dtype)

    # Se non ho valori validi creo array di zeri
    return np.zeros(shape, dtype=dtype)


def set_current_action_from_native_value(env, current_action: np.ndarray, device_id: str, setpoint_name: str, native_value: float) -> Optional[dict]:
    action_mapping = get_action_mapping(env) # Recupero la mappa dei setpoint controllabili da agente
    key = (device_id, setpoint_name) #creo chiave per cercare setpoint
    info = action_mapping.get(key) #cerco setpoint nella mappa con la chiave
    if info is None:
        return None

    normalizer = info.get("normalizer") #prendo normalizzatore (se c'è)
    if normalizer is None:
        return None

    agent_value = float(normalizer.agent_value(float(native_value))) #normalizzo il valore reale
    spec = env.action_spec() #applico i limiti dell'atcion space
    try:
        min_v = float(np.asarray(spec.minimum)[info["index"]])
        max_v = float(np.asarray(spec.maximum)[info["index"]])
        agent_value = float(np.clip(agent_value, min_v, max_v)) #Limito il valore dentro i limiti consentiti
    except Exception:
        agent_value = float(np.clip(agent_value, -1.0, 1.0))

    current_action[info["index"]] = agent_value #scrivo il valore nell'action vector
    return {
        "channel": "env_action_vector",
        "index": info["index"],
        "field_id": str(info["field_id"]),
        "agent_value": agent_value,
    }


def apply_previous_overrides(building): #applico gli overrides precedenti dell'utente

    if getattr(building, "_runtime_override_hook_installed", False):
        return
    original_request_action = building.request_action #1. Salvo azione originale

    def patched_request_action(action_request): #2. applico overrides sui device individuati
        response = original_request_action(action_request)
        overrides = getattr(building, "_user_runtime_overrides", {})
        device_map = getattr(building, "_device_map", {})
        for (device_id, setpoint_name), value in list(overrides.items()):
            try:
                device = device_map[device_id]
                device.set_action(setpoint_name, float(value), building.current_timestamp)
            except Exception as e:
                print(f"[WARN] Override runtime non applicato: {device_id} | {setpoint_name} = {value} | {e}")
        return response

    #3. Salvo overrides riapplicati
    building._original_request_action_for_runtime_overrides = original_request_action
    building.request_action = patched_request_action
    building._runtime_override_hook_installed = True


def apply_user_overrides(building, user_overrides): # Applico l'override appena richiesto dall'utente
    setattr(building, "_user_runtime_overrides", user_overrides)
    device_map = getattr(building, "_device_map", {})
    for (device_id, setpoint_name), value in user_overrides.items():
        try:
            device = device_map[device_id]
            device.set_action(setpoint_name, float(value), building.current_timestamp)
            print(f"Override diretto riapplicato: {device_id} | {setpoint_name} = {value}")
        except Exception as e:
            print(f"[WARN] Override diretto non applicato: {device_id} | {setpoint_name} = {value} | {e}")


def apply_setpoint_change(env, building, user_overrides: dict, current_action: np.ndarray, write_result: dict) -> dict:
    #applica modifica richiesta dall'utente (tramite action vector o override)
    device_id = write_result["device_id"]
    setpoint_name = write_result["setpoint_name"]
    value = float(write_result["requested_value"])

    # se possibile uso action vector, altrimenti applico overrides manuali
    action_update = set_current_action_from_native_value(env, current_action, device_id, setpoint_name, value)
    if action_update is not None:
        user_overrides.pop((device_id, setpoint_name), None)
        channel = action_update["channel"]
        agent_value = action_update["agent_value"]
    else:
        user_overrides[(device_id, setpoint_name)] = value
        setattr(building, "_user_runtime_overrides", user_overrides)
        channel = "runtime_direct_device_set_action"
        agent_value = None
    apply_user_overrides(building, user_overrides)
    readback_value = read_back_point(building, device_id, setpoint_name)
    return {
        "channel": channel,
        "device_id": device_id,
        "setpoint_name": setpoint_name,
        "requested_value": value,
        "agent_value": agent_value,
        "readback_value": readback_value,
    }


def json_overrides(user_overrides: dict, current_action: np.ndarray, env) -> str:
    # conversione in json delle azioni "manuali" e "via agente" per riportarle nel log
    action_mapping = get_action_mapping(env)
    action_items = []
    for (device_id, setpoint_name), info in action_mapping.items():
        try:
            action_items.append({
                "device_id": device_id,
                "setpoint_name": setpoint_name,
                "index": info["index"],
                "agent_value": float(current_action[info["index"]]),
            })
        except Exception:
            pass
    direct_items = [
        {"device_id": d, "setpoint_name": s, "value": float(v)}
        for (d, s), v in user_overrides.items()
    ]
    return json.dumps({
        "env_action_vector": action_items,
        "direct_runtime_overrides": direct_items,
    }, ensure_ascii=False)

def safe_float(x) -> float: #converto i valori di SbSim in float
    try:
        return float(x)
    except Exception:
        return float("nan")

def electricity_kwh_step(reward_info, step_seconds: float) -> float:
    #Calcolo energia elettrica consumata nello step usando RewardInfo (W -> kWh)
    step_hours = step_seconds / 3600 #'step su ora': 1 step / 1 ora = 5 minuti / 60 minuti
    total_watt = 0.0

    # Air handlers:  1. blower electrical 2. air conditioning
    for ahu in reward_info.air_handler_reward_infos.values():
        total_watt += safe_float(ahu.blower_electrical_energy_rate)
        total_watt += safe_float(ahu.air_conditioning_electrical_energy_rate)

    # Boilers: pump electrical
    for boiler in reward_info.boiler_reward_infos.values():
        total_watt += safe_float(boiler.pump_electrical_energy_rate)
    return (total_watt / 1000.0) * step_hours  # conversione in kWh


def gas_kwh_step(reward_info, step_seconds: float) -> float:
    #Calcolo energia gas consumata nello step usando RewardInfo (W -> kWh)
    step_hours = step_seconds / 3600 #'step su ora': 1 step / 1 ora = 5 minuti / 60 minuti
    total_watt = 0.0

    # Boilers: natural gas heating power
    for boiler in reward_info.boiler_reward_infos.values():
        total_watt += safe_float(boiler.natural_gas_heating_energy_rate)

    return (total_watt / 1000.0) * step_hours  # kWh


def building_avg_temp_c_step(reward_info) -> float:
    #Temperatura media dell'edificio nello step corrente in Celsius (media delle temperature di tutte le zone)
    temps_c = []
    for _, z in reward_info.zone_reward_infos.items():
        temp_k = safe_float(z.zone_air_temperature)
        temps_c.append(temp_k - 273.15)
    return sum(temps_c) / len(temps_c) if temps_c else float("nan")



def simulate_one_day(env, out_csv: str) -> Dict[str, Any]:
    """
    Esegue 1 giorno completo e salva i log per step.
    Ritorna un riepilogo aggregato.

    - se l'utente modifica un setpoint nello spazio azioni RL, aggiorna current_action;
    - se l'utente modifica un setpoint fuori dallo spazio azioni RL, lo riapplica dopo setup_step_sim().
    """

    logs: List[StepLog] = []
    env.reset()

    #costruisco le variabili che mi servono tramite le apposite funzioni
    building = env.building
    devices = view_env(building)
    zone_map = build_zone_map(building)
    system_prompt = build_system_prompt()

    #applico gli overrides effettuati in precedenza prima di leggere o scrivere 
    user_overrides = {}
    setattr(building, "_user_runtime_overrides", user_overrides)
    apply_previous_overrides(building)

    steps = int(env.steps_per_episode)
    step_seconds = 300
    current_action = make_initial_action(env)

    print("\n===== ACTION SPACE SBSIM =====")
    action_mapping = get_action_mapping(env)
    if action_mapping:
        for (device_id, setpoint_name), info in action_mapping.items():
            print(f"action[{info['index']}] -> {device_id} | {setpoint_name}")
    else:
        print("Nessuna action mappata nell'Environment.")

    total_reward = 0.0
    total_electricity_cost = 0.0
    total_gas_cost = 0.0
    total_carbon = 0.0
    total_electricity_kwh = 0.0
    total_gas_kwh = 0.0
    sum_avg_temp_c = 0.0

    #interfaccia con utente
    for i in range(steps):
        print(f"\n--- STEP {i + 1}/{steps} ---")
        user_text = input("Comando utente (INVIO per nessuna azione, 'exit' per uscire): ").strip()

        if user_text.lower() == "exit":
            print("Simulazione interrotta dall'utente.")
            break

        # applico function calling a partire dal prompt aggregato
        applied_writes = []
        if user_text:
            try:
                result = interpreta_prompt(user_text, system_prompt, building, devices, zone_map)
                print("Risultato function calling:", result)

                for res in result.get("writes_applied", []):
                    record = apply_setpoint_change(env, building, user_overrides, current_action, res)
                    applied_writes.append(record)
                    print(
                        f"[OVERRIDE DINAMICO] "
                        f"channel={record['channel']} "
                        f"device={record['device_id']} "
                        f"setpoint={record['setpoint_name']} "
                        f"requested={record['requested_value']} "
                        f"agent_value={record['agent_value']} "
                        f"readback={record['readback_value']}"
                    )

            except (json.JSONDecodeError, ValidationError) as e:
                print("Errore: JSON restituito da Qwen non valido.")
                print("Dettagli:", e)
            except Exception as e:
                print("Errore durante function calling:")
                print(e)

        if user_overrides:
            apply_user_overrides(building, user_overrides) #riapplico overrides aggiornati prima dell'avanzamento dello step

        ts = env.step(current_action)
        apply_previous_overrides(building)

        r = safe_float(ts.reward)
        reward_info = env.building.reward_info
        reward_response = env.reward_function.compute_reward(reward_info)

        electricity_kwh = electricity_kwh_step(reward_info, step_seconds)
        gas_kwh = gas_kwh_step(reward_info, step_seconds)
        avg_temp_c = building_avg_temp_c_step(reward_info)

        electricity_cost = safe_float(reward_response.electricity_energy_cost)
        gas_cost = safe_float(reward_response.natural_gas_energy_cost)
        carbon = safe_float(reward_response.carbon_emitted)
        occupancy = safe_float(reward_response.total_occupancy)
        prod_regret = safe_float(reward_response.productivity_regret)
        current_ts = str(env.current_simulation_timestamp)
        
        #determino log
        logs.append(
            StepLog(
                step_idx=i,
                timestamp=current_ts,
                reward=r,
                electricity_cost=electricity_cost,
                gas_cost=gas_cost,
                carbon=carbon,
                electricity_kwh=electricity_kwh,
                gas_kwh=gas_kwh,
                avg_temp_c=avg_temp_c,
                occupancy=occupancy,
                productivity_regret=prod_regret,
                active_overrides_json=json_overrides(user_overrides, current_action, env),
                applied_writes_json=json.dumps(applied_writes, ensure_ascii=False),
            )
        )
        
        #aggiorno metriche cumulative
        total_reward += r
        total_electricity_cost += electricity_cost
        total_gas_cost += gas_cost
        total_carbon += carbon
        total_electricity_kwh += electricity_kwh
        total_gas_kwh += gas_kwh
        sum_avg_temp_c += avg_temp_c
        
        #riporto metriche dello step su console
        print(
            f"DATI [{(i+1):03d}/{steps}] t={current_ts} "
            f"r={r:.4f} cost_el={electricity_cost:.6f} cost_gas={gas_cost:.6f} "
            f"co2={carbon:.4f} el_kwh={electricity_kwh:.6f} gas_kwh={gas_kwh:.6f} "
            f"avg_temp: {avg_temp_c:.2f}C"
        )
    
    #salvo logs di tutti gli step in un csv
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "step_idx", "timestamp", "reward",
            "electricity_cost", "gas_cost", "carbon",
            "electricity_kwh", "gas_kwh", "avg_temp_c",
            "occupancy", "productivity_regret",
            "active_overrides_json", "applied_writes_json"
        ])
        for row in logs:
            w.writerow([
                row.step_idx, row.timestamp, row.reward,
                row.electricity_cost, row.gas_cost, row.carbon,
                row.electricity_kwh, row.gas_kwh, row.avg_temp_c,
                row.occupancy, row.productivity_regret,
                row.active_overrides_json, row.applied_writes_json
            ])
    
    #report finale cumulativo su console
    steps_executed = len(logs)
    summary = {
        "steps": steps_executed,
        "total_reward": total_reward,
        "total_electricity_cost": total_electricity_cost,
        "total_gas_cost": total_gas_cost,
        "total_cost": total_electricity_cost + total_gas_cost,
        "total_carbon": total_carbon,
        "total_electricity_kwh": total_electricity_kwh,
        "total_gas_kwh": total_gas_kwh,
        "total_avg_temp_c": sum_avg_temp_c / steps_executed if steps_executed > 0 else float("nan"),
        "csv": out_csv,
    }
    print("CSV completato. Simulazione terminata")
    return summary


if __name__ == "__main__":
    
    #importo file di config e creo file .csv
    gin_file = "smart_control/configs/resources/sb1/train_sim_configs/sim_config_1_day.gin"
    out_csv = "logs/one_day_metrics.csv"
    env = load_environment(gin_file)

    #TEST VARI
    #export_building_map_to_excel(env.building, "logs/building_map.xlsx")
    #test.test_setpoint_vav(env)
    #test.test_setpoint_boiler(env)
    #test.dump_devices_and_setpoints(env)
    #test.sanity_check_building(env)
    #test.dump_action_mapping(env)
    #test.dump_real_action_names(env)
    
    #eseguo simulazione
    summary = simulate_one_day(env, out_csv)
    print("\n=== SIMULAZIONE 1 GIORNO (1 episodio) ===")
    for k, v in summary.items():
        print(f"{k}: {v}")

    #PER ESEGUIRE TEST DI COMPARAZIONE SU UN SETPOINT
    """
    quick_two_value_override_test(
        gin_file=gin_file,
        device_prefix="air_handler",
        setpoint_name="supply_air_cooling_temperature_setpoint",
        value_a=285.0,
        value_b=305.0,
        n_steps=96,
    )
    """




