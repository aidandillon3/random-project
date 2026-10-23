"""Checks that a generated vehicle's powertrain is one connected tree the game can run."""

ROOTS = {"combustionEngine", "electricMotor"}
TWO_OUTPUTS = {"differential"}


def devices(parts):
    """[(type, name, input name, input index, options)] of every part's powertrain rows."""
    out = []
    for d in parts.values():
        for r in (d.get("powertrain") or [])[1:]:
            if isinstance(r, list):
                out.append((r[0], r[1], r[2], r[3], r[4] if len(r) > 4 and isinstance(r[4], dict) else {}))
    return out


def problems(parts, wheel_names, driven=None):
    """Sentences about what is wrong (empty when the tree is sound).

    wheel_names: every wheel of the vehicle; driven: the ones that must be driven (default: all of them
    connected to a shaft, at most once each)."""
    out = []
    devs = devices(parts)
    by_name = {}
    for t, n, src, idx, opts in devs:
        if n in by_name:
            out.append("device %s is defined twice" % n)
        by_name[n] = (t, src, idx, opts)
    roots = [n for n, (t, src, idx, o) in by_name.items() if src == "dummy"]
    if len(roots) != 1 or by_name[roots[0]][0] not in ROOTS:
        out.append("one engine or motor should drive everything, found %s" % roots)
        return out
    root = roots[0]
    used = {}
    for n, (t, src, idx, opts) in by_name.items():
        if n == root:
            continue
        if src not in by_name:
            out.append("%s takes its input from %s, which does not exist" % (n, src))
            continue
        ports = 2 if by_name[src][0] in TWO_OUTPUTS else 1
        if idx not in range(1, ports + 1):
            out.append("%s uses output %s of %s, which has %d" % (n, idx, src, ports))
        if (src, idx) in used:
            out.append("%s and %s share output %s of %s" % (used[(src, idx)], n, idx, src))
        used[(src, idx)] = n
    # everything hangs from the root
    for n in by_name:
        seen, cur = set(), n
        while cur != root and cur in by_name and cur not in seen:
            seen.add(cur)
            cur = by_name[cur][1]
        if cur != root:
            out.append("%s is not driven by %s" % (n, root))
    wheels = [opts.get("connectedWheel") for t, n, src, idx, opts in devs if opts.get("connectedWheel")]
    for w in wheels:
        if w not in wheel_names:
            out.append("a shaft turns wheel %s, which does not exist" % w)
    if len(set(wheels)) != len(wheels):
        out.append("a wheel is turned by two shafts")
    if driven is not None and set(wheels) != set(driven):
        out.append("driven wheels %s, expected %s" % (sorted(wheels), sorted(driven)))
    # what feeds the root
    sections = {}
    for d in parts.values():
        sections.update(d)
    dev = sections.get(root) or {}
    store = dev.get("energyStorage")
    store = [store] if isinstance(store, str) else store or []
    kinds = {}
    for d in parts.values():
        for r in (d.get("energyStorage") or [])[1:]:
            if isinstance(r, list) and len(r) >= 2:
                kinds[r[1]] = r[0]
    want = "electricBattery" if by_name[root][0] == "electricMotor" else "fuelTank"
    if not store or any(kinds.get(s) != want for s in store):
        out.append("%s should run on a %s, has %s" % (root, want, {s: kinds.get(s) for s in store}))
    if by_name[root][0] == "electricMotor":
        vc = sections.get("vehicleController") or {}
        if vc.get("shiftLogicName") != "electricMotor" or root not in (vc.get("motorNames") or []):
            out.append("the vehicle controller does not run the motor: %s" % vc)
        if any(t in ("manualGearbox", "automaticGearbox") for t, *_ in devs):
            out.append("an electric motor has no gearbox")
    return out
