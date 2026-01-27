from pydantic import BaseModel, ValidationError
from typing import Optional, Literal
import json
import ollama

from smart_control.environment import environment_test_utils
from smart_control.proto import smart_control_building_pb2

class Intent(BaseModel): # modello base per le istanze di 'Intent'
    type: Literal["command", "query"]
    zone_id: Optional[str] = None
    device_id: Optional[str] = None
    # per query:
    measurement_name: Optional[str] = None
    # per command:
    setpoint_name: Optional[str] = None
    value: Optional[float] = None


def build_zone_map(building) -> dict:
    #ritorno per ogni zona la lista di devices disponibili
    zone_map = {}
    for z in building.zones:
        zone_map[z.zone_id] = list(z.devices)
    return zone_map


def view_env(building: environment_test_utils.SimpleBuilding) -> dict:
    #ritorno per ogni device la lista di sensori e attuatori
    devices = {}
    for dev in building.devices:
        device_id = dev.device_id
        setpoints = list(dev.action_fields.keys())        # controllabili
        measurements = list(dev.observable_fields.keys()) # osservabili
        devices[device_id] = {"setpoints": setpoints, "measurements": measurements}
    return devices


def system_prompt(devices: dict, zone_map: dict) -> str:
    #mappatura campi disponibili per ogni singolo device
    dev_lines = []
    for device_id, d in devices.items():
        dev_lines.append(
            f"- {device_id}: setpoints={d['setpoints']}, measurements={d['measurements']}"
        )

    #mappatura devices disponibili per ogni singola zona
    zone_lines = []
    for zone_id, devices in zone_map.items():
        zone_lines.append(f"- {zone_id}: devices={devices}")

    #prompt in ingresso per LLM
    SYSTEM_PROMPT = f"""
    Sei un interprete che traduce richieste in linguaggio naturale in JSON per un simulatore SbSim.

    DEVI rispondere con SOLO JSON valido (nessun testo extra, niente markdown).
    Schema JSON:
    
    QUERY:
    {{
      "type": "query",
      "zone_id": "<opzionale: zone_1 o zone_2>",
      "device_id": "<opzionale se c'è zone_id>",
      "measurement_name": "<obbligatorio>",
      "setpoint_name": null,
      "value": null
    }}
    
    COMMAND:
    {{
      "type": "command",
      "zone_id": "<opzionale: zone_1 o zone_2>",
      "device_id": "<opzionale se c'è zone_id>",
      "measurement_name": null,
      "setpoint_name": "<obbligatorio>",
      "value": <numero>
    }}
    
    REGOLE FORTI (IMPORTANTISSIMO):
    - Non inventare mai device_id, measurement_name, setpoint_name.
    - Usa SOLO i device/campi elencati sotto.
    - Se l’utente dice "zona (x)", imposta zone_id rispettivamente a "zone_(x)" (esempio: zona 1 -> zone_id = zone_1)
    - Se c’è zone_id e l’utente non specifica device_id, scegli un device_id CHE ESISTE in quella zona.
    - Se manca un dato obbligatorio, compila comunque JSON ma metti null nei campi mancanti (device_id può essere null solo se zone_id è presente).
    
    Zone disponibili e device per zona:
    {"\n".join(zone_lines)}
    
    Device e campi disponibili:
    {"\n".join(dev_lines)}
    """.strip()

    return SYSTEM_PROMPT


def interpreta_prompt(user_text: str, system_prompt: str) -> Intent:
    resp = ollama.chat( #metodo di ollama per interagire con LLM
        model="qwen2.5:7b",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        format="json"
    )
    content = resp["message"]["content"]
    print("DEBUG LLM RAW:", content) #per stampare il json che viene inviato
    data = json.loads(content) # trasformo il json in diz
    return Intent(**data)


def esegui_intent(building, intent: Intent, devices: dict, zone_map: dict) -> dict:

    #CONTROLLI DI COERENZA

    #1. Se c'è zone_id ma manca device_id, deduci device_id
    if intent.zone_id and not intent.device_id:
        devices_in_zone = zone_map.get(intent.zone_id)

        #1.1 Se la zona indicata dall'utente non esiste
        if not devices_in_zone:
            return {
                "error": f"Zona '{intent.zone_id}' non valida.",
                "valid_zones": list(zone_map.keys()),
            }

        #Scegli un device della zona coerente col tipo di richiesta
        chosen = None
        if intent.type == "command":
            for d in devices_in_zone:
                if d in devices and len(devices[d]["setpoints"]) > 0:
                    chosen = d
                    break
        else:  # query (si possono fare sia su setpoints che su measurements)
            for d in devices_in_zone:
                if d in devices and (len(devices[d]["measurements"]) > 0 or len(devices[d]["setpoints"]) > 0):
                    chosen = d
                    break
        #errore se non trova devices coerenti con la zona richiesta
        if not chosen:
            return {
                "error": f"Nessun device utilizzabile trovato in {intent.zone_id} per {intent.type}.",
                "devices_in_zone": devices_in_zone,
            }
        intent.device_id = chosen

    #2. Restituisci errore se device_id non è deducibile o è inesistente
    if not intent.device_id: #2.1 device_id non deducibile
        return {
            "error": "Manca device_id (e non è stata fornita una zone_id valida per dedurlo).",
            "suggerimento": "Specifica device_id oppure zona (zone_1/zone_2).",
        }
    if intent.device_id not in devices: #2.2 device_id inesistente
        return {
            "error": f"Device '{intent.device_id}' non esiste.",
            "valid_devices": list(devices.keys()),
            "zones": zone_map,  # utile per capire dove sono i device
        }

    #3. Se c'è zone_id, verifica coerenza device-zona
    if intent.zone_id:
        devices_in_zone = zone_map.get(intent.zone_id, [])
        if intent.device_id not in devices_in_zone:
            return {
                "error": f"Device '{intent.device_id}' non appartiene a {intent.zone_id}.",
                "devices_in_zone": devices_in_zone,
            }
    #--FINE CONTROLLI

    # QUERY
    if intent.type == "query":
        if not intent.measurement_name and intent.setpoint_name: #se l'utente dice 'dimmi setpoint 1', restituisco lo stato del setpoint 1 come fosse una misurazione
            intent.measurement_name = intent.setpoint_name

        if not intent.measurement_name: #errore se measurement_name = none
            return {
                "error": "Query senza measurement_name.",
                "valid_measurements": devices[intent.device_id]["measurements"],
            }

        valid_measurements = set(devices[intent.device_id]["measurements"])
        valid_readable_setpoints = set(devices[intent.device_id]["setpoints"])  # nel SimpleBuilding li leggo come measurement_name

        #verifico se il campo esiste sul device specifico
        if intent.measurement_name not in valid_measurements and intent.measurement_name not in valid_readable_setpoints:
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

    #validazione setpoints (controllo che il setpoint sia disponibile all'interno dell'intent_device)
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


if __name__ == "__main__":
    print("Avvio SbSim + Qwen ...")
    building = environment_test_utils.SimpleBuilding() #creo building di simulazione
    devices = view_env(building)
    zone_map = build_zone_map(building)
    system_prompt = system_prompt(devices, zone_map)
    while True:
        user = input(">>> ").strip()
        if user.lower() in {"exit", "quit"}:
            break
        try:
            intent = interpreta_prompt(user, system_prompt)
        except (json.JSONDecodeError, ValidationError) as e: #se il json creato non è valido
            print("Errore: LLM ha restituito JSON non valido o schema sbagliato.")
            print("Dettagli:", e)
            continue
        except Exception as e: #se fallisce la chiamata a Qwen
            print("Errore chiamata LLM:", e)
            continue

        result = esegui_intent(building, intent, devices, zone_map)
        print("Risultato:", result)

