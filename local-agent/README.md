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
4. Want it to see the screen? If the page says there's no vision file, press **Find vision file
   online**. It works out which base model yours is built on (for example
   `Swift-1.5-Qwen3.8-27B-...` is built on Qwen3.8-27B), finds that model's vision file (`mmproj`)
   on Hugging Face, checks it fits, and downloads it next to your model. It's usually under 1 GB.
5. Press **Start model**. When it says **Ready**, go to **Chat** and ask for something.

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
| Git and GitHub | clone, status, diff, log, branch, commit, pull, push; list your repos; list, read and open pull requests (and ask for reviewers); list and open issues; comment |
| Coding | work in a private git copy of a repo, run the tests until they pass, map a project's classes and functions, several exact edits at once, a syntax check after every edit |
| Memory | save notes, search notes and your indexed files, forget notes, add a folder to the index |
| Thinking | a to-do plan shown on the right, ask you a question, wait, hand a big search to a helper agent with its own fresh memory |

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

## Coding mode

Ask it to change code in a git repository ("fix the crash when the config file is missing in
my body-tracker repo") and it works the way Claude Code does:

1. It makes a **private copy** of the repo on a new branch (a git worktree, in
   `.worktrees` inside the work folder). Your own checkout and files aren't touched.
2. It maps the code (classes and functions per file) to find its way around.
3. It edits, and every edit gets a quick syntax check so a broken file is noticed at once.
4. It **runs the tests** (pytest, npm, cargo, go, dotnet, maven or gradle, found
   automatically), reads the failures, fixes them, and runs them again until they pass. It
   isn't allowed to delete or weaken tests to get there. If it's stuck after six failed runs
   in a row, it stops and tells you what fails.
5. It reviews its own diff, commits, and if you asked, pushes and opens a pull request.

Packages your project needs (node_modules, .venv) aren't copied into the private copy. For
Python it uses your project's `.venv` automatically; for other languages it installs them in
the copy when the tests need them. Turn coding mode off in Settings if you don't want it.

## Long-term memory

The AI keeps short notes between chats: your preferences, where your projects are, what fixed
a problem. To keep it fast and focused it is **not** shown all of them. With each message it
sees only the few notes that clearly match what you asked (plus any you pin), and it can
search the rest whenever it needs to. After bigger tasks it asks itself what's worth
remembering and saves up to three notes, updating old ones instead of piling up duplicates.

It also keeps a **search index of your files** (the work folder, plus any folders you add on
the Memory page), so it can find "the budget spreadsheet" or "where I handle logins" by what's
inside them. The index updates itself every 10 minutes and only re-reads changed files.

Everything is in `data/memory.db` on your PC. The **Memory** page lets you read, search,
edit, pin and delete notes, and pick which folders are indexed. Your old `memory.md` notes
are moved in automatically.

## Unity

It can work inside the **Unity Editor** while you have your project open: see the scene, place
and change objects, set component values, make prefabs and materials, write scripts and fix
their compile errors, play the game for a few seconds to test, and read the console.

1. Open your project in Unity.
2. Ask, for example: "set up Unity for my project in D:/Games/MyGame". It adds one small editor
   script (`Assets/Editor/LocalAgent/`) to the project. Click into the Unity window once so
   Unity loads it. (Or copy the two files from this app's `unity` folder there yourself.)
3. Ask for things: "add a red cube that falls when the game starts", "why does the player fall
   through the floor?", "make an enemy prefab that walks toward the player".

How it works on a script change: it writes the `.cs` file, makes Unity compile it, reads the
errors, fixes them until it compiles, adds the script to an object, then plays the game for a
few seconds and checks the console and a picture of the game view.

Good to know:
- Its scene changes go through Unity's undo, so **Ctrl+Z in Unity** takes them back. It doesn't
  save the scene unless you ask (or it finishes a task you gave it).
- Changes made during play mode are lost when play stops, like in Unity itself, so it stops
  play mode before editing.
- The editor script only listens on your own PC (127.0.0.1) and needs a random key that Unity
  writes into the project's `Library` folder. Delete `Assets/Editor/LocalAgent` to remove it.
- If Unity is minimized and answers slowly, set Unity's *Preferences > General > Interaction
  Mode* to *No Throttling*.
- Pictures of the game view come from the cameras, so screen-space overlay UI doesn't show.
- Turn the Unity tools off in Settings if you don't use Unity; they take a little room in
  the model's context.

## GitHub

It can work on your GitHub repositories the way Claude does here: clone one, make a branch,
change code, commit, push, and open a pull request (optionally asking someone to review).

1. Install **Git for Windows** from [git-scm.com](https://git-scm.com/download/win) if you don't
   have it.
2. Sign in, one of two ways:
   - **Token:** Settings > GitHub > **Make a token...** opens GitHub. Make a fine-grained token,
     pick the repositories it may use, and give it *Contents: read and write*, *Pull requests: read
     and write* and *Issues: read and write*. Paste it into the Token box and press **Check sign-in**.
   - **GitHub CLI:** if you have `gh`, run `gh auth login` once in a terminal. The app uses that login.
3. Ask, for example: "clone aidandillon3/random-project, fix the typo in the README, and open a
   pull request".

The token is saved only in `data/settings.json` on your PC (that folder is never committed). It's
sent only to GitHub, and the git and GitHub tools blank it out of anything they show the model.

## Safety: what it asks before doing

At the top of the chat, **Permissions**:

| Mode | Asks before |
|---|---|
| Read only | Never changes anything (refuses edits, commands, clicks) |
| **Ask before risky actions** (default) | Every file change, command, click or keypress |
| Auto-approve file edits in the work folder | Commands, clicks, and edits outside the work folder |
| Full auto | Only clearly dangerous commands (formatting, recursive deletes, registry deletes, shutdown...) |

Pushing code and posting to GitHub (pull requests, issues, comments) count as a separate kind of
action that **asks even in Full auto**, because it goes out under your name. You can turn that off
in Settings > GitHub. It never force-pushes.

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
| **Qwen3.6-27B** (IQ4_XS, "MTP" upload) | Dense | Yes | ~14 GB, fits | Hard coding/reasoning; MTP speed-up about 1.3-2x |
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
| MTP speculative decoding | Models with built-in prediction layers write about 1.3-2x faster | Speed-up option (works with vision) |
| Draft model | A tiny model of the same family guesses ahead | Speed-up option |
| N-gram speculation | Reuses text already in the chat; fast when editing code | Speed-up option |
| Prompt caching | The server remembers the conversation so each step only reads what's new; the app avoids rewriting old messages so the cache keeps working | Always on |
| Quant choice | Q4_K_M is the sweet spot; IQ4_XS ~10% smaller; below Q3 gets dumb | Shown per model |

Smarter answers: set **Thinking** (Settings > Answers) to Medium or High for hard tasks, Off or
Low for quick clicks, and press **Use the model
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
| Unity tools say no editor is running | Open the project in Unity, then ask it to set up Unity for that project. If the scripts have compile errors, choose *Ignore* when Unity offers Safe Mode |
| It can't see screenshots | Press **Find vision file online** on the Model server page, and keep "Let it see images" on |

## For the curious

- `localagent/agent.py`: the loop, approvals, context summarizing, memory recall.
- `localagent/memory.py`: notes and the file index (SQLite full-text search).
- `localagent/tools/code.py`: coding mode (private copies, test loop, code map).
- `localagent/tools/unity.py` + `unity/LocalAgentBridge.cs`: the Unity tools and the editor script they talk to.
- `localagent/tools/`: every tool; adding one is a single decorated function.
- `localagent/launcher.py` + `gguf.py`: reads the model file header and plans GPU/RAM placement.
- `tests/`: run `python -m pytest tests` (uses a fake model server; no GPU needed).
