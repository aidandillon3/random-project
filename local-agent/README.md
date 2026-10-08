# Local Agent: a Claude-style AI helper that runs fully on your own PC

A chat app where a local AI model can actually do things on your computer: read and edit files,
run commands, **see your screen**, click, type, open programs, search the web, plan multi-step
work and remember things between chats. Nothing goes to the cloud: the model runs on your GPU.

It also starts the model for you, with the speed settings worked out for an
**RTX 5080 (16 GB) + 64 GB RAM**.

## Install (once)

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/). Tick
   *"Add python.exe to PATH"* during install.
2. Make sure your **NVIDIA driver is up to date** (GeForce Experience / NVIDIA App). The newest
   llama.cpp builds need a recent driver.
3. Double-click **`setup.bat`** in this folder and wait for "Setup done".
4. Double-click **`run.bat`**.

## First run

1. Go to the **Model server** page.
2. Press **Download / update llama.cpp**. It grabs the newest NVIDIA build (a few hundred MB).
3. Press **Models folder...** and pick the folder with your `.gguf` models (LM Studio's folder is
   found by itself). Choose a model in the list. The page shows what's inside it and whether it
   fits on your card.
4. Press **Start model**. When it says **Ready**, go to **Chat** and ask for something.

Already using **LM Studio** or **Ollama**? Skip the Model server page: in **Settings > Connection**
pick it from the list, and load the model there with tool calling on. (Ollama's model files
aren't `.gguf` files you can browse to, so for Ollama models use Ollama itself.)

## What it can do

| Group | Tools |
|---|---|
| Files | read (with line numbers, also shows images), write, exact edit, list, find by name, search inside files, move, copy, delete (to the Recycle Bin) |
| Commands | run PowerShell commands, run long ones in the background and check on them, run Python scripts |
| Screen | screenshot (with the mouse pointer marked), zoom in to read small text, click, double/right click, drag, scroll, type, key shortcuts, list and focus windows, open programs/files/links, read the buttons and menus of the active window and click them by name, clipboard |
| Web | search (DuckDuckGo, no account), read pages |
| Thinking | a to-do plan shown on the right, long-term memory, ask you a question, wait, hand a big search to a helper agent with its own fresh memory |

Things it does the way Claude does:

- **Works in a loop**: it calls tools, reads the results, and keeps going until the job is done
  (up to 150 steps before it checks in with you).
- **Checks its own work** on screen: every click or keypress returns a fresh screenshot.
- **Plans** bigger jobs with a visible to-do list.
- **Keeps long tasks going**: when the conversation gets near the model's memory limit, older
  parts are summarized automatically, and old screenshots are dropped.
- **Recovers from model mistakes**: broken tool-call JSON is repaired or sent back to fix, tool
  calls written as text are picked up, it's told when it repeats the same action, and servers
  without proper tool support get the tools described in the prompt instead.
- **Reads `AGENT.md`**: put standing instructions in a file called `AGENT.md` in the work folder.
- Paste images into the chat (Ctrl+V), attach files, or press **Add screenshot**.

## Safety: what it asks before doing

At the top of the chat, **Permissions**:

| Mode | Asks before |
|---|---|
| Read only | Never changes anything (refuses edits, commands, clicks) |
| **Ask before risky actions** (default) | Every file change, command, click or keypress |
| Auto-approve file edits in the work folder | Commands, clicks, and edits outside the work folder |
| Full auto | Only clearly dangerous commands (formatting, recursive deletes, registry deletes, shutdown...) |

In the approval box you can also pick **Allow all ... for this chat**, or **Deny** and type what
it should do instead. Deleted files go to the Recycle Bin.

**Emergency stop:** press **Stop** (or Esc), or slam the mouse into any screen corner while it is
controlling the mouse.

The app minimizes itself while the AI uses the screen, so it doesn't click on its own window
(Settings > Screen to change that). If you have two monitors, you can also put the app on one and
tell it to work on the other (Settings > Screen > Which screen).

## Models for a 5080 (16 GB) + 64 GB RAM

| Model | Type | Sees the screen | On your PC | Pick it for |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (Q4_K_M) | MoE, 3B active | Yes | ~21 GB: attention on the GPU, some experts in RAM. Fast | **Best all-rounder for this app** |
| **Qwen3.8-27B** (IQ4_XS or Q3_K_M) | Dense | Check the model page | Q4_K_M (~18 GB) spills into RAM and slows down; IQ4_XS/Q3 fit | Smartest model that runs locally, slower |
| **Qwen3.6-27B** (IQ4_XS, "MTP" upload) | Dense | Yes | ~14 GB, fits | Hard coding/reasoning; MTP speed-up ~2x without vision |
| **gpt-oss-20b** (MXFP4) | MoE, 3.6B active | No | ~13 GB, fully on the GPU. Very fast | Fast text and coding work |

Search Hugging Face for the name plus "GGUF" (bartowski and unsloth make good ones), and for
screen vision download the matching **mmproj** file into the same folder as the model.

What's checked and what's guessed: Qwen3.6-35B-A3B, Qwen3.6-27B and Qwen3.8-27B were checked
against model pages in October 2026, and the 27B file sizes come from a real quant table. Speed
numbers in the app are estimates from memory bandwidth, not measurements on your PC. Whether
Qwen3.8-27B includes vision wasn't confirmed. Any model with tool calling works here.

## The speed tricks (all set up for you)

| Trick | What it does | In this app |
|---|---|---|
| Flash attention | Faster attention, less memory | Always on |
| KV cache q8_0 | Context uses half the memory, practically no quality loss | Default; q4_0 option |
| MoE expert offload + auto-fit | Rarely-used experts go to RAM, everything else stays on the GPU, so a 35B MoE runs fast on 16 GB | Automatic (llama.cpp `--fit`, or the app's own plan on older builds) |
| MTP speculative decoding | Models with built-in prediction layers write ~2x faster | Speed-up option (turns vision off) |
| Draft model | A tiny model of the same family guesses ahead | Speed-up option |
| N-gram speculation | Reuses text already in the chat; fast when editing code | Speed-up option |
| Prompt caching | The server remembers the conversation so each step only reads what's new; the app avoids rewriting old messages so the cache keeps working | Always on |
| Quant choice | Q4_K_M is the sweet spot; IQ4_XS ~10% smaller; below Q3 gets dumb | Shown per model |

Smarter answers: turn **Thinking** on (Settings) for hard tasks, and press **Use the model
maker's recommended values** for sampling.

## Other ways to run it

- **`run-terminal.bat`**: the same agent in a terminal window.
- **`get-llama-cpp.bat`**: download llama.cpp without opening the app.
- Your settings, chats and memory live in the `data` folder here; delete it to start fresh.

## If something's wrong

| Problem | Fix |
|---|---|
| "Can't reach the model server" | Start the model on the Model server page, or check the address in Settings |
| The server stops right after starting | Read the log under the Start button. "out of memory": pick a shorter context or q4_0 KV cache. "CUDA driver version is insufficient": update the NVIDIA driver |
| It writes tool calls as plain text, or ignores tools | Use a model made for tool calling (the list above). The app already starts llama.cpp with `--jinja`, which most models need |
| Clicks land in the wrong place | Settings > Screen > Click coordinates: try the other option. Turning on the grid also helps smaller models aim |
| It can't see screenshots | The model needs its mmproj file next to it, and "Let it see images" on |

## For the curious

- `localagent/agent.py`: the loop, approvals, context summarizing.
- `localagent/tools/`: every tool; adding one is a single decorated function.
- `localagent/launcher.py` + `gguf.py`: reads the model file header and plans GPU/RAM placement.
- `tests/`: run `python -m pytest tests` (uses a fake model server; no GPU needed).
