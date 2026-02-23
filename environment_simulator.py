# simulate_one_day.py
import os
import csv
from dataclasses import dataclass
from typing import Dict, Any, List
import gin
import numpy as np
import pandas as pd
import test_utils as test

from smart_control.environment import environment as sc_environment
from smart_control.proto import smart_control_building_pb2

# gli import seguenti NON servono "per usarli nel codice", ma per farli conoscere a Gin
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
    """
    logs: List[StepLog] = []

    # reset environment (stato iniziale)
    env.reset()

    # numero step dell'episodio (per 1 giorno --> 24h / 5min = 288 step)
    steps = int(env.steps_per_episode)

    #numero di secondi per step (da passare alle funzioni di calcolo dei consumi)
    step_seconds = 300

    # azione "neutra": array di zeri con la shape richiesta dall'action_spec
    action_spec = env.action_spec()
    zero_action = np.zeros(action_spec.shape, dtype=action_spec.dtype)

    #inizializzo le variabili cumulative
    total_reward = 0.0
    total_electricity_cost = 0.0
    total_gas_cost = 0.0
    total_carbon = 0.0
    total_electricity_kwh = 0.0
    total_gas_kwh = 0.0
    sum_avg_temp_c = 0.0

    for i in range(steps):

        # 1) avanza di un passo
        ts = env.step(zero_action)

        # 2) reward del timestep (float)
        r = safe_float(ts.reward)
        '''
        Reward positivo = ottimo comfort/basso consumo energetico
        Reward negativo = pessimo cofort/alto consumo energetico
        '''

        # 3) metriche “ufficiali” del reward (derivate dalle regole del progetto)
        reward_info = env.building.reward_info
        reward_response = env.reward_function.compute_reward(reward_info)

        # 4a) Calcolo dei consumi e della temperatura media
        electricity_kwh = electricity_kwh_step(reward_info, step_seconds)
        gas_kwh = gas_kwh_step(reward_info, step_seconds)
        avg_temp_c = building_avg_temp_c_step(reward_info)

        # 4b) Converto i valori in float/nan
        electricity_cost = safe_float(reward_response.electricity_energy_cost)
        gas_cost = safe_float(reward_response.natural_gas_energy_cost)
        carbon = safe_float(reward_response.carbon_emitted)
        occupancy = safe_float(reward_response.total_occupancy)
        prod_regret = safe_float(reward_response.productivity_regret)

        # timestamp simulazione (stringa)
        current_ts = str(env.current_simulation_timestamp)

        # 5) Inserisco i dati dello step nel log
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
            )
        )

        #6) Aggiorno i totali
        total_reward += r
        total_electricity_cost += electricity_cost
        total_gas_cost += gas_cost
        total_carbon += carbon
        total_electricity_kwh += electricity_kwh
        total_gas_kwh += gas_kwh
        sum_avg_temp_c += avg_temp_c

        #7) Riporto le metriche relative al singolo step
        print(
            f"[{(i+1):03d}/{steps}] t={current_ts} "
            f"r={r:.4f} cost_el={electricity_cost:.6f} cost_gas={gas_cost:.6f} "
            f"co2={carbon:.4f} el_kwh={electricity_kwh:.6f} gas_kwh={gas_kwh:.6f} "
            f"avg_temp: {avg_temp_c:.2f}C"
        )

    # salva CSV
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "step_idx", "timestamp", "reward",
            "electricity_cost", "gas_cost", "carbon",
            "electricity_kwh", "gas_kwh", "avg_temp_c",
            "occupancy", "productivity_regret"
        ])
        for row in logs:

            w.writerow([
                row.step_idx, row.timestamp, row.reward,
                row.electricity_cost, row.gas_cost, row.carbon,
                row.electricity_kwh, row.gas_kwh, row.avg_temp_c,
                row.occupancy, row.productivity_regret
            ])

    summary = {
        "steps": steps,
        "total_reward": total_reward,
        "total_electricity_cost": total_electricity_cost,
        "total_gas_cost": total_gas_cost,
        "total_cost": total_electricity_cost + total_gas_cost,
        "total_carbon": total_carbon,
        "total_electricity_kwh": total_electricity_kwh,
        "total_gas_kwh": total_gas_kwh,
        "total_avg_temp_c": sum_avg_temp_c / steps,
        "csv": out_csv,
    }
    print("CSV completato. Simulazione terminata")
    return summary


if __name__ == "__main__":
    
    gin_file = "smart_control/configs/resources/sb1/train_sim_configs/sim_config_1_day.gin"
    out_csv = "logs/one_day_metrics.csv"

    env = load_environment(gin_file)

    #TEST VARI
    #test.test_setpoint_vav(env)
    #test.test_setpoint_boiler(env)
    #test.dump_devices_and_setpoints(env)
    #test.sanity_check_building(env)
    #test.dump_action_mapping(env)
    #test.dump_real_action_names(env)

    summary = simulate_one_day(env, out_csv)

    print("\n=== SIMULAZIONE 1 GIORNO (1 episodio) ===")
    for k, v in summary.items():
        print(f"{k}: {v}")
