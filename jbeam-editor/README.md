# JBeam Editor

Make and change BeamNG.drive vehicles without writing JBeam by hand. It shows a
vehicle in 3D the way the game builds it (same slots, variables and node
offsets), and covers everything from a quick lift kit to building a whole new
vehicle:

- **Garage**: lift kits, bigger wheels and tires, wheel spacers, engine power,
  brakes and paint, with the result in 3D straight away.
- **Vehicle Builder**: a complete, drivable vehicle from sliders: body style,
  length, wheelbase, track width, ride height, suspension, engine and gearbox.
- **Scrap Mechanic import**: turn a Scrap Mechanic creation into a BeamNG
  vehicle, with its wheels, suspension, steering and engine.
- **3D Model Workshop**: model a bumper, roof rack or light bar in the editor and
  bolt it on with a node structure fitted to its shape. Pick the vehicle nodes it
  bolts to by clicking them, and choose how strong the mount is (light, normal,
  strong or welded, or your own spring and breaking force).
- **Import a 3D model** (`.obj`, `.glb`, `.gltf`, `.stl`, `.dae`) as a new part.
- **Parts, Tuning and Structure** for detailed work: the game's parts menu and
  tuning sliders, and direct editing of nodes, beams, collision triangles and
  weights.

## Starting it

You need Python 3.8 or newer (from python.org or the Microsoft Store). Nothing
else is required. Extract the zip first; the launcher can't run from inside it.
If it finds no Python it says so; when installing from python.org, tick
"Add python.exe to PATH".

1. Double-click **Start JBeam Editor.bat** (or run `python run.py`).
2. It opens in its own window. With [pywebview](https://pywebview.flowrl.com/)
   installed (Settings > *Install native window*) that is a native window;
   otherwise Edge or Chrome in app mode (no tabs or address bar), or your browser
   if neither is there. Closing the window stops the editor.
3. The first time, check **Settings**: it finds your BeamNG user folder
   (`%LOCALAPPDATA%\BeamNG\BeamNG.drive\current`), the game folder and Scrap
   Mechanic by itself when they are installed with Steam.

Settings > *Make desktop and Start menu shortcuts* gives it an icon like any
other program.

## The home screen

Pick what you want to do: modify a vehicle, build a new one, import from Scrap
Mechanic, model a part, import a 3D model, or edit JBeam in detail. Below that is
every vehicle the editor found (the game's and your mods), with search and a
filter for your own.

## Garage

The easy way to change a vehicle. Everything shows in 3D at once and **Save my
version** writes it as your own config of the car in a mod folder; the original
is never changed.

- **Quick builds**: one click sets up an off-road, monster truck, lowered street,
  rally or drag version (lift, tire sizes, stance, power and brakes, sized from
  the vehicle's own wheels); everything stays adjustable.
- **Lift & suspension**: a lift kit moves the suspension down on longer brackets,
  the way a real one does, so springs, shocks and steering keep working. Front and
  rear can differ. Body lifts too.
- **Wheels & tires**: tire size and width (also sizes the vehicle has no part
  for; bigger tires get heavier like real ones), rims, spacers and track width.
- Trucks with more than two axles get these settings for every axle (Front 1,
  Front 2, Rear 1, Rear 2, ...). Axles that hang on one suspension part, like a
  tandem on shared springs, lift together.
- **Engine**: more or less power and a different rev limit, with the torque curve
  (electric vehicles: the motor's power and top speed).
- **Brakes** and **Paint**, and **Fine tuning** for the vehicle's own sliders.
- **Drop test**: drops the vehicle onto the ground in a quick physics simulation
  and tells you whether it sits at the right height and holds together.

The Builder and the importer also tell you how hard a vehicle can corner before
it tips over, and warn when it would roll before its tires slide.

## Vehicle Builder

Start from a body style (sedan, hatchback, coupe, wagon, SUV, off-roader,
pickup, van, roadster, supercar) and shape it with sliders: length, width,
height, wheelbase, overhangs, track width, ground clearance, hood and roof
lines, tire and rim sizes, independent or solid-axle suspension, wheel travel,
spring and damper settings, anti-roll bars, engine power and character, gearbox,
driven wheels, weight and brakes. Or make it electric: a motor with full torque
from standstill, no gearbox, and a battery in the floor that lowers the centre
of gravity; the Builder shows how far a charge goes.

Everything is generated for you: the body and its node structure, double
wishbone or solid axle suspension with real pivot points, coilovers and bump
stops, wheels, engine, gearbox, differentials, fuel tank, and head, tail, brake
and reverse lights that light up the road at night. Spring rates come
from the weight and how soft you want the ride; gearing from the power and top
speed. **Create vehicle** writes it as a mod; open it again in the Builder later
to change it.

## Scrap Mechanic import

Pick a creation you saved with the lift (or one from the Steam Workshop), see it
as a BeamNG vehicle straight away, adjust it and create it.

- Wheels on bearings become real BeamNG wheels on coilovers, in the same places.
  Scrap Mechanic off-road suspension starts soft with long travel, sport
  suspension firm and short, and wheels without suspension get a short, firm
  ride.
- The wheels on bearings your **driver's seat** is connected to steer; the ones
  your **engines** are connected to are driven, through proper differentials
  (any number of axles, lone wheels too). The front is found from the steered
  wheels. Electric engines become an electric motor with a battery.
- The blocks become a stiff node structure that follows their shape, drawn in
  the creation's own colours; the main colour is the paint, so you can repaint it
  in the game. Parts on the suspension move with their wheel, and two wheels on
  one sprung beam become a solid axle. Pistons are fixed at their saved length;
  spare wheels stay part of the body. Head and tail lights are added on the
  front and back so it can be driven at night.
- Settings: size (1× is Scrap Mechanic's own scale), which way is the front,
  steering, wheel travel, springs, tires, petrol or electric, power, driven
  wheels, gearbox, top speed and weight.

The editor reads the game's own part list to know what every part is, and the
part lists of Workshop mods a creation uses. If Scrap Mechanic is not found, set
its folder in Settings (the one with `Data` and `Survival` in it).

## Structure (detailed editing)

- Select (click, Shift+click, box drag), then drag the handles to **move** (W),
  **stretch** (R) or **turn** (E) what you selected; add nodes that connect to
  what you had selected; join nodes with beams; make collision triangles;
  delete; mirror left and right sides.
- **Reshape parts with their model.** With *Model follows* on, the 3D model
  bends and stretches with the nodes as you move them, and saving writes the new
  shape into the model's `.dae` too (the game doesn't move a model when only its
  nodes move). To make a bed wider, use ⋯ > *Select its nodes* in Parts, pick
  *Stretch* and drag the side handle, or type a factor under *Stretch* in the
  properties. A model that belongs to the game is copied into your mod under a
  new name instead of being changed.
- Every node, beam and triangle option is editable, and any part's text can be
  opened and edited directly.
- **Weights and stability**: each node is checked against the stiffness and
  damping of its beams at the game's 2000 Hz physics step and coloured green,
  yellow or red. Auto-weight fixes unstable nodes or spreads a total mass so
  stiff areas get more weight.
- **New parts and upfits**: copy a part (also one from the game, into your own
  mod), start from a braced box, or start empty; add slots for upfits.
- **How firmly a part is mounted** (⋯ next to its slot): makes the beams that
  hold it to the rest of the vehicle stiffer and harder to break, up to never
  breaking.
- **Check** runs a full checker over your mod; **Export** packs it into a zip.

Press **Help** in the editor for the mouse and keyboard controls.

## Safety

- Only files in your own mod folders (`mods\unpacked\...`, plus any extra
  folders you add in Settings) are ever changed. The game folder, zip mods and
  Scrap Mechanic are only read.
- Edits change just the rows you touched; the rest of the file keeps its layout
  and comments. Every file is parsed again before it is written.
- A copy of every file is saved in `data\backups\` before it is changed,
  including vehicles the Builder or the importer update.
- Settings, caches, backups and exports live in the `data` folder next to the
  editor.

## Notes

- After saving, press Ctrl+R in the game to reload a spawned vehicle, or Ctrl+L
  to reload the vehicle list after creating a new vehicle.
- The editor's 3D view shows plain material colours, paint and glass, but not
  textures; wheels made by `pressureWheels` without a model are drawn as simple
  cylinders.
- The stability colours and the drop test are a guide worked out from the
  official vehicles, not the game's own simulation.

## Tests

`python -m unittest discover -s tests` runs the Python tests,
`node --test tests/js/*.test.mjs` the JavaScript ones, and
`node tests/ui_smoke.cjs` drives the whole editor in a headless browser against
a stand-in BeamNG and Scrap Mechanic install.
