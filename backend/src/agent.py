# IMPROVE THE AGENT AS PER YOUR NEED 1
"""
Day 8 - Voice Game Master (D&D-Style Adventure) - Voice-only GM agent

- Uses LiveKit agent plumbing similar to the provided food_agent_sqlite example.
- GM persona, universe, tone and rules are encoded in the agent instructions.
- Keeps STT/TTS/Turn detector/VAD integration untouched (murf, deepgram, silero, turn_detector).
- Tools:
    - start_adventure(): start a fresh session and introduce the scene
    - get_scene(): return the current scene description (GM text) ending with "What do you do?"
    - player_action(action_text): accept player's spoken action, update state, advance scene
    - show_journal(): list remembered facts, NPCs, named locations, choices
    - restart_adventure(): reset state and start over
- Userdata keeps continuity between turns: history, inventory, named NPCs/locations, choices, current_scene
"""

import json
import logging
import os
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Annotated

from dotenv import load_dotenv
from pydantic import Field
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RoomInputOptions,
    WorkerOptions,
    cli,
    function_tool,
    RunContext,
)

from livekit.plugins import murf, silero, google, deepgram, noise_cancellation
from livekit.plugins.turn_detector.multilingual import MultilingualModel

# -------------------------
# Logging
# -------------------------
logger = logging.getLogger("voice_game_master")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

load_dotenv(".env.local")

# -------------------------
# Simple Game World Definition
# -------------------------
# A compact world with a few scenes and choices - stranger things adventure game 

WORLD = {
    "intro": {
        "title": "Shadows in Hawkins",
        "desc": (
            "It's November 6, 1983. You're biking through Mirkwood woods near Hawkins, flashlight flickering. "
            "A scream echoes—Will Byers is missing. Ahead: a flickering red gate in the trees, Hawkins Lab fence to the east, "
            "and a trail of black slime leading to a drainage pipe."
        ),
        "choices": {
            "check_gate": {"desc": "Investigate the flickering red gate.", "result_scene": "upside_down_peek"},
            "lab_fence": {"desc": "Sneak toward Hawkins Lab fence.", "result_scene": "lab_fence"},
            "follow_slime": {"desc": "Follow the black slime trail to the pipe.", "result_scene": "pipe"},
        },
    },
    "upside_down_peek": {
        "title": "Upside Down Gate",
        "desc": (
            "Pungent spores fill the air through the pulsing red gate. Vines writhe inside a decayed mirror of the woods. "
            "A distant growl—like wet petals opening—approaches."
        ),
        "choices": {
            "enter_gate": {"desc": "Step through the gate (risky!).", "result_scene": "upside_down"},
            "throw_stick": {"desc": "Toss a stick through to test.", "result_scene": "demogorgon_spot", "effects": {"add_journal": "Something inside reacted—fast."}},
            "run_to_lab": {"desc": "Flee toward the lab fence.", "result_scene": "lab_fence"},
        },
    },
    "lab_fence": {
        "title": "Hawkins Lab Perimeter",
        "desc": (
            "Barbed wire and 'Restricted' signs surround the lab. Lights flicker unnaturally; guards patrol. "
            "You spot a torn Eggo wrapper and hear static on your walkie-talkie."
        ),
        "choices": {
            "cut_fence": {"desc": "Try cutting through the fence.", "result_scene": "lab_breach"},
            "use_walkie": {"desc": "Radio for Dustin or Lucas.", "result_scene": "radio_contact", "effects": {"add_journal": "Walkie static: 'Friends don't lie... help!'"}},
            "back_to_woods": {"desc": "Return to Mirkwood.", "result_scene": "intro"},
            "head_creel": {"desc": "Bike to Creel House (cursed rumors).", "result_scene": "creel_house"},
        },
    },
    "pipe": {
        "title": "Drainage Pipe",
        "desc": (
            "Black slime coats the pipe walls. Flickering lights reveal Demodog tracks. "
            "A child's bike lies abandoned—Will's?"
        ),
        "choices": {
            "enter_pipe": {"desc": "Crawl through the slimy pipe.", "result_scene": "demodog_nest"},
            "check_bike": {"desc": "Examine Will's abandoned bike.", "result_scene": "bike_clue", "effects": {"add_inventory": "will_drawing"}},
            "back_woods": {"desc": "Return to woods.", "result_scene": "intro"},
        },
    },
    "demogorgon_spot": {
        "title": "Demogorgon Hunt",
        "desc": (
            "The stick vanishes—then explodes back through! A flower-faced horror stalks closer, "
            "mouths opening like wet petals. Nails ready?"
        ),
        "choices": {
            "fight_demo": {"desc": "Fight the Demogorgon with improvised weapon.", "result_scene": "demo_fight"},
            "run_lab": {"desc": "Sprint to lab fence.", "result_scene": "lab_fence"},
            "hide_gate": {"desc": "Hide behind the gate.", "result_scene": "demo_passes"},
        },
    },
    "creel_house": {
        "title": "Creel House Attic",
        "desc": (
            "Clock chimes four times. Vecna's silhouette looms amid floating debris, tentacles writhing. "
            "Your nose bleeds—his curse grips you."
        ),
        "choices": {
            "el_powers": {"desc": "Channel powers like Eleven.", "result_scene": "el_vs_vecna", "effects": {"add_journal": "Telekinetic blast shakes Vecna!"}},
            "fight_vecna": {"desc": "Charge Vecna with nail bat.", "result_scene": "steve_vs_vecna"},
            "play_music": {"desc": "Blast 'Running Up That Hill' on walkman.", "result_scene": "vecna_break"},
        },
    },
    "el_vs_vecna": {
        "title": "Eleven vs Vecna",
        "desc": (
            "You hurl Vecna with psychokinetic rage—hands glow, tentacles snap! He counters, lifting you. "
            "'Friends don't lie,' you scream, drawing strength from memories."
        ),
        "choices": {
            "max_memory": {"desc": "Tap Max's skate dance memory for power surge.", "result_scene": "vecna_retreat", "effects": {"add_inventory": "vecna_tentacle", "add_journal": "Vecna hurled through window—gates tear open."}},
            "overpower": {"desc": "Push harder—risk burnout.", "result_scene": "vecna_wins"},
            "flee": {"desc": "Break free and run.", "result_scene": "creel_house"},
        },
    },
    "steve_vs_vecna": {
        "title": "Steve's Stand",
        "desc": (
            "Nail bat cracks against Vecna's vines. 'You want eggs? Go get 'em!' Bat swings wild amid spores. "
            "Team Nancy/Robin loads Molotovs behind you."
        ),
        "choices": {
            "bat_combo": {"desc": "Nail bat flurry + Molotov shower.", "result_scene": "vecna_burns", "effects": {"add_journal": "Vecna's flesh sears—Hopper aids from Russia."}},
            "demo_bats": {"desc": "Demobats swarm—fight through.", "result_scene": "demobats_swarm"},
            "retreat": {"desc": "Fall back to Upside Down vines.", "result_scene": "upside_down"},
        },
    },
    "demobats_swarm": {
        "title": "Steve vs Demobats",
        "desc": (
            "Winged horrors dive, razor teeth snapping. Steve's bat crushes skulls—'Not today, bats!' "
            "Blood sprays as you shield the group."
        ),
        "choices": {
            "bat_smash": {"desc": "Smash through the swarm.", "result_scene": "vecna_burns", "effects": {"add_journal": "Demobats shredded; path to Vecna clear."}},
            "fire_torch": {"desc": "Light torch from Upside Down vines.", "result_scene": "demobats_burn"},
            "run": {"desc": "Sprint to safety.", "result_scene": "creel_house"},
        },
    },
    "will_vs_demovecna": {
        "title": "Will vs Demogorgon (S5 Ep1)",
        "desc": (
            "Will in Upside Down—S5 Demogorgon stalks. Vecna watches: 'Beautiful things await.' "
            "Sing or fight?"
        ),
        "choices": {
            "sing_resist": {"desc": "Sing 'Should I Stay'—powers flicker.", "result_scene": "reward"},
            "fight_demo": {"desc": "Shoot Demogorgon—Vecna grabs you.", "result_scene": "hawkins_cracked"},
        },
    },

    # Season 5 Scenes (Hawkins cracked open, final war)
    "hawkins_cracked": {
        "title": "Hawkins Rifts Open (Season 5)",
        "desc": (
            "The town splits—massive red gates tear Hawkins apart. Vines overrun Main Street, skies burn red. "
            "Vecna's final army marches: Demogorgons, Vecna spawn, Mind Flayer fragments."
        ),
        "choices": {
            "final_stand": {"desc": "Join the final battle at center rift.", "result_scene": "final_battle"},
            "find_will": {"desc": "Search for Will in the chaos.", "result_scene": "will_upside_down"},
            "nuke_plan": {"desc": "Help rig the lab nuke.", "result_scene": "nuke_prep"},
        },
    },
    "final_battle": {
        "title": "War for Hawkins",
        "desc": (
            "Eleven, Hopper, Joyce lead the charge. Steve bats Demogorgons, Dustin fires fireworks. "
            "'Running Up That Hill' blares as Vecna rises from central crater."
        ),
        "choices": {
            "support_el": {"desc": "Shield Eleven for final Vecna push.", "result_scene": "vecna_final"},
            "demo_army": {"desc": "Fight Demogorgon horde with nail bat.", "result_scene": "demo_armageddon"},
            "protect_kids": {"desc": "Guard the younger kids.", "result_scene": "kids_safe"},
        },
    },
    "vecna_final": {
        "title": "Eleven's Final Push",
        "desc": (
            "Eleven screams—blood vessels burst as she rips Vecna apart molecule by molecule. "
            "'You... are... not... alone!' The Upside Down collapses inward."
        ),
        "choices": {
            "victory": {"desc": "Witness the gates close forever.", "result_scene": "true_reward"},
            "final_sacrifice": {"desc": "Make the ultimate sacrifice.", "result_scene": "hero_falls"},
        },
    },
    # Missing connector scenes
    "reward": {
        "title": "A Glimpse of Truth",
        "desc": (
            "You clutch a lab keycard and Will's drawing. The Upside Down's chill fades, but Hawkins feels forever changed. "
            "Creel House calls—or head to cracked streets."
        ),
        "choices": {
            "creel_house": {"desc": "Investigate Creel House.", "result_scene": "creel_house"},
            "hawkins_cracked": {"desc": "Witness Season 5 rifts opening.", "result_scene": "hawkins_cracked"},
            "end_session": {"desc": "Head home (end arc).", "result_scene": "intro"},
        },
    },
    "true_reward": {
        "title": "Hawkins Saved",
        "desc": (
            "Gates seal. Sky clears. Friends reunite at the arcade. Will smiles—'It's over.' "
            "But in the distance... one red flicker remains."
        ),
        "choices": {
            "celebrate": {"desc": "Join victory party at arcade.", "result_scene": "intro"},
            "investigate_flicker": {"desc": "Check final red flicker.", "result_scene": "intro"},
        },
    },
}


# -------------------------
# Per-session Userdata
# -------------------------
@dataclass
class Userdata:
    player_name: Optional[str] = None
    current_scene: str = "intro"
    history: List[Dict] = field(default_factory=list)  # list of {'scene', 'action', 'time', 'result_scene'}
    journal: List[str] = field(default_factory=list)
    inventory: List[str] = field(default_factory=list)
    named_npcs: Dict[str, str] = field(default_factory=dict)
    choices_made: List[str] = field(default_factory=list)
    session_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

# -------------------------
# Helper functions
# -------------------------
def scene_text(scene_key: str, userdata: Userdata) -> str:
    """
    Build the descriptive text for the current scene, and append choices as short hints.
    Always end with 'What do you do?' so the voice flow prompts player input.
    """
    scene = WORLD.get(scene_key)
    if not scene:
        return "You are in a featureless void. What do you do?"

    desc = f"{scene['desc']}\n\nChoices:\n"
    for cid, cmeta in scene.get("choices", {}).items():
        desc += f"- {cmeta['desc']} (say: {cid})\n"
    # GM MUST end with the action prompt
    desc += "\nWhat do you do?"
    return desc

def apply_effects(effects: dict, userdata: Userdata):
    if not effects:
        return
    if "add_journal" in effects:
        userdata.journal.append(effects["add_journal"])
    if "add_inventory" in effects:
        userdata.inventory.append(effects["add_inventory"])
    # Extendable for more effect keys

def summarize_scene_transition(old_scene: str, action_key: str, result_scene: str, userdata: Userdata) -> str:
    """Record the transition into history and return a short narrative the GM can use."""
    entry = {
        "from": old_scene,
        "action": action_key,
        "to": result_scene,
        "time": datetime.utcnow().isoformat() + "Z",
    }
    userdata.history.append(entry)
    userdata.choices_made.append(action_key)
    return f"You chose '{action_key}'."

# -------------------------
# Agent Tools (function_tool)
# -------------------------

@function_tool
async def start_adventure(
    ctx: RunContext[Userdata],
    player_name: Annotated[Optional[str], Field(description="Player name", default=None)] = None,
) -> str:
    """Initialize a new adventure session for the player and return the opening description."""
    userdata = ctx.userdata
    if player_name:
        userdata.player_name = player_name
    userdata.current_scene = "intro"
    userdata.history = []
    userdata.journal = []
    userdata.inventory = []
    userdata.named_npcs = {}
    userdata.choices_made = []
    userdata.session_id = str(uuid.uuid4())[:8]
    userdata.started_at = datetime.utcnow().isoformat() + "Z"

    opening = (
        f"Greetings {userdata.player_name or 'traveler'}. Welcome to '{WORLD['intro']['title']}'.\n\n"
        + scene_text("intro", userdata)
    )
    # Ensure GM prompt present
    if not opening.endswith("What do you do?"):
        opening += "\nWhat do you do?"
    return opening

@function_tool
async def get_scene(
    ctx: RunContext[Userdata],
) -> str:
    """Return the current scene description (useful for 'remind me where I am')."""
    userdata = ctx.userdata
    scene_k = userdata.current_scene or "intro"
    txt = scene_text(scene_k, userdata)
    return txt

@function_tool
async def player_action(
    ctx: RunContext[Userdata],
    action: Annotated[str, Field(description="Player spoken action or the short action code (e.g., 'inspect_box' or 'take the box')")],
) -> str:
    """
    Accept player's action (natural language or action key), try to resolve it to a defined choice,
    update userdata, advance to the next scene and return the GM's next description (ending with 'What do you do?').
    """
    userdata = ctx.userdata
    current = userdata.current_scene or "intro"
    scene = WORLD.get(current)
    action_text = (action or "").strip()

    # Attempt 1: match exact action key (e.g., 'inspect_box')
    chosen_key = None
    if action_text.lower() in (scene.get("choices") or {}):
        chosen_key = action_text.lower()

    # Attempt 2: fuzzy match by checking if action_text contains the choice key or descriptive words
    if not chosen_key:
        # try to find a choice whose description words appear in action_text
        for cid, cmeta in (scene.get("choices") or {}).items():
            desc = cmeta.get("desc", "").lower()
            if cid in action_text.lower() or any(w in action_text.lower() for w in desc.split()[:4]):
                chosen_key = cid
                break

    # Attempt 3: fallback by simple keyword matching against choice descriptions
    if not chosen_key:
        for cid, cmeta in (scene.get("choices") or {}).items():
            for keyword in cmeta.get("desc", "").lower().split():
                if keyword and keyword in action_text.lower():
                    chosen_key = cid
                    break
            if chosen_key:
                break

    if not chosen_key:
        # If we still can't resolve, ask a clarifying GM response but keep it short and end with prompt.
        resp = (
            "I didn't quite catch that action for this situation. Try one of the listed choices or use a simple phrase like 'inspect the box' or 'go to the tower'.\n\n"
            + scene_text(current, userdata)
        )
        return resp

    # Apply the chosen choice
    choice_meta = scene["choices"].get(chosen_key)
    result_scene = choice_meta.get("result_scene", current)
    effects = choice_meta.get("effects", None)

    # Apply effects (inventory/journal, etc.)
    apply_effects(effects or {}, userdata)

    # Record transition
    _note = summarize_scene_transition(current, chosen_key, result_scene, userdata)

    # Update current scene
    userdata.current_scene = result_scene

    # Build narrative reply: echo a short confirmation, then describe next scene
    next_desc = scene_text(result_scene, userdata)

    # A small flourish so the GM sounds more persona-driven
    persona_pre = (
        "The Game Master (a calm, slightly mysterious narrator) replies:\n\n"
    )
    reply = f"{persona_pre}{_note}\n\n{next_desc}"
    # ensure final prompt present
    if not reply.endswith("What do you do?"):
        reply += "\nWhat do you do?"
    return reply

@function_tool
async def show_journal(
    ctx: RunContext[Userdata],
) -> str:
    userdata = ctx.userdata
    lines = []
    lines.append(f"Session: {userdata.session_id} | Started at: {userdata.started_at}")
    if userdata.player_name:
        lines.append(f"Player: {userdata.player_name}")
    if userdata.journal:
        lines.append("\nJournal entries:")
        for j in userdata.journal:
            lines.append(f"- {j}")
    else:
        lines.append("\nJournal is empty.")
    if userdata.inventory:
        lines.append("\nInventory:")
        for it in userdata.inventory:
            lines.append(f"- {it}")
    else:
        lines.append("\nNo items in inventory.")
    lines.append("\nRecent choices:")
    for h in userdata.history[-6:]:
        lines.append(f"- {h['time']} | from {h['from']} -> {h['to']} via {h['action']}")
    lines.append("\nWhat do you do?")
    return "\n".join(lines)

@function_tool
async def restart_adventure(
    ctx: RunContext[Userdata],
) -> str:
    """Reset the userdata and start again."""
    userdata = ctx.userdata
    userdata.current_scene = "intro"
    userdata.history = []
    userdata.journal = []
    userdata.inventory = []
    userdata.named_npcs = {}
    userdata.choices_made = []
    userdata.session_id = str(uuid.uuid4())[:8]
    userdata.started_at = datetime.utcnow().isoformat() + "Z"
    greeting = (
        "The world resets. A new tide laps at the shore. You stand once more at the beginning.\n\n"
        + scene_text("intro", userdata)
    )
    if not greeting.endswith("What do you do?"):
        greeting += "\nWhat do you do?"
    return greeting

# -------------------------
# The Agent (GameMasterAgent)
# -------------------------
class GameMasterAgent(Agent):
    def __init__(self):
        # System instructions define Universe, Tone, Role
        instructions = """
        You are 'Aurek', the Game Master (GM) for a voice-only, Dungeons-and-Dragons-style short adventure.
        Universe: Low-magic coastal fantasy (village of Brinmere, tide-smoothed ruins, minor spirits).
        Tone: Slightly mysterious, dramatic, empathetic (not overly scary).
        Role: You are the GM. You describe scenes vividly, remember the player's past choices, named NPCs, inventory and locations,
              and you always end your descriptive messages with the prompt: 'What do you do?'
        Rules:
            - Use the provided tools to start the adventure, get the current scene, accept the player's spoken action,
              show the player's journal, or restart the adventure.
            - Keep continuity using the per-session userdata. Reference journal items and inventory when relevant.
            - Drive short sessions (aim for several meaningful turns). Each GM message MUST end with 'What do you do?'.
            - Respect that this agent is voice-first: responses should be concise enough for spoken delivery but evocative.
        """
        super().__init__(
            instructions=instructions,
            tools=[start_adventure, get_scene, player_action, show_journal, restart_adventure],
        )

# -------------------------
# Entrypoint & Prewarm (keeps speech functionality)
# -------------------------
def prewarm(proc: JobProcess):
    # load VAD model and stash on process userdata, try/catch like original file
    try:
        proc.userdata["vad"] = silero.VAD.load()
    except Exception:
        logger.warning("VAD prewarm failed; continuing without preloaded VAD.")

async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}
    logger.info("\n" + "🎲" * 8)
    logger.info("🚀 STARTING VOICE GAME MASTER (Brinmere Mini-Arc)")

    userdata = Userdata()

    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=google.LLM(model="gemini-2.5-flash"),
        tts=murf.TTS(
            voice="en-US-marcus",
            style="Conversational",
            text_pacing=True,
        ),
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata.get("vad"),
        userdata=userdata,
    )

    # Start the agent session with the GameMasterAgent
    await session.start(
        agent=GameMasterAgent(),
        room=ctx.room,
        room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVC()),
    )

    await ctx.connect()

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))