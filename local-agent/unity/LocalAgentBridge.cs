// Local Agent bridge for the Unity Editor.
//
// Lets the Local Agent app look at and change the open Unity project: the scene hierarchy,
// components and their settings, prefabs, materials, the console, compiling, play mode and
// pictures of the scene. It listens on 127.0.0.1 only, and every request must carry a random
// key that this script writes into the project's Library folder and the user's temp folder,
// so other programs and web pages can't drive the editor.
//
// Install: put this file in Assets/Editor/LocalAgent/ (the app's unity_install_bridge tool does
// that). It starts by itself whenever Unity loads scripts. Remove the file to switch it off.

#if UNITY_EDITOR
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEditor;
using UnityEditor.Compilation;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;
using D = System.Collections.Generic.Dictionary<string, object>;
using L = System.Collections.Generic.List<object>;
using Object = UnityEngine.Object;

namespace LocalAgent
{
    [InitializeOnLoad]
    public static class LocalAgentBridge
    {
        public const string BridgeVersion = "1";
        const int FirstPort = 6461, LastPort = 6480;
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        static TcpListener listener;
        static Thread acceptThread;
        static volatile bool running;
        static string token;
        static int port;
        static string projectRoot;

        class Pending
        {
            public D req;
            public string resp;
            public bool cancelled;
            public readonly ManualResetEvent done = new ManualResetEvent(false);
        }

        static readonly Queue<Pending> queue = new Queue<Pending>();
        static readonly object qlock = new object();

        static LocalAgentBridge()
        {
            projectRoot = Directory.GetParent(Application.dataPath).FullName.Replace('\\', '/');
            token = SessionState.GetString("LocalAgent.token", "");
            if (token == "")
            {
                token = Guid.NewGuid().ToString("N");
                SessionState.SetString("LocalAgent.token", token);
            }
            RestoreLogs();
            RestoreCompile();
            Application.logMessageReceivedThreaded += OnLog;
            CompilationPipeline.compilationStarted += OnCompileStarted;
            CompilationPipeline.assemblyCompilationFinished += OnAssemblyCompiled;
            CompilationPipeline.compilationFinished += OnCompileFinished;
            AssemblyReloadEvents.beforeAssemblyReload += BeforeReload;
            EditorApplication.quitting += Quit;
            EditorApplication.update += Pump;
            StartListening();
        }

        // ------------------------------------------------------------------ networking
        static void StartListening()
        {
            int last = SessionState.GetInt("LocalAgent.port", 0);
            var ports = new List<int>();
            if (last != 0) ports.Add(last);
            for (int p = FirstPort; p <= LastPort; p++) if (p != last) ports.Add(p);
            foreach (int p in ports)
            {
                try
                {
                    var l = new TcpListener(IPAddress.Loopback, p);
                    l.Start();
                    listener = l;
                    port = p;
                    break;
                }
                catch (SocketException) { }
            }
            if (listener == null)
            {
                Debug.LogWarning("[Local Agent] No free port between " + FirstPort + " and " + LastPort + "; the bridge is off.");
                return;
            }
            SessionState.SetInt("LocalAgent.port", port);
            running = true;
            acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "LocalAgentBridge" };
            acceptThread.Start();
            WriteDiscovery();
        }

        static void StopListening()
        {
            running = false;
            try { if (listener != null) listener.Stop(); } catch (Exception) { }
            listener = null;
            lock (qlock)
            {
                foreach (var p in queue)
                {
                    p.resp = Json.Write(Fail("Unity is reloading scripts; try again in a few seconds."));
                    p.done.Set();
                }
                queue.Clear();
            }
        }

        static void BeforeReload()
        {
            SaveLogs();
            StopListening();
        }

        static void Quit()
        {
            StopListening();
            foreach (var f in DiscoveryFiles())
            {
                try { File.Delete(f); } catch (Exception) { }
            }
        }

        static string[] DiscoveryFiles()
        {
            uint h = 2166136261;
            foreach (char c in projectRoot.ToLowerInvariant()) { h ^= c; h *= 16777619; }
            return new[]
            {
                Path.Combine(projectRoot, "Library/LocalAgentBridge.json"),
                Path.Combine(Path.Combine(Path.GetTempPath(), "localagent-unity"), h.ToString("x8") + ".json"),
            };
        }

        static void WriteDiscovery()
        {
            var info = new D
            {
                {"project", projectRoot}, {"port", port}, {"token", token}, {"unity", Application.unityVersion},
                {"pid", System.Diagnostics.Process.GetCurrentProcess().Id}, {"bridge", BridgeVersion},
            };
            string text = Json.Write(info);
            foreach (var f in DiscoveryFiles())
            {
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(f));
                    File.WriteAllText(f, text);
                }
                catch (Exception e) { Debug.LogWarning("[Local Agent] Couldn't write " + f + ": " + e.Message); }
            }
        }

        static void AcceptLoop()
        {
            while (running)
            {
                TcpClient client;
                try { client = listener.AcceptTcpClient(); }
                catch (Exception) { break; }
                ThreadPool.QueueUserWorkItem(_ => Serve(client));
            }
        }

        static void Serve(TcpClient client)
        {
            using (client)
            {
                try
                {
                    client.ReceiveTimeout = 15000;
                    var stream = client.GetStream();
                    var buf = new MemoryStream();
                    int b;
                    while ((b = stream.ReadByte()) != -1 && b != '\n')
                    {
                        buf.WriteByte((byte)b);
                        if (buf.Length > 16 * 1024 * 1024) return;
                    }
                    string resp = Handle(Encoding.UTF8.GetString(buf.ToArray()));
                    byte[] bytes = Encoding.UTF8.GetBytes(resp + "\n");
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush();
                }
                catch (Exception) { }
            }
        }

        static string Handle(string line)
        {
            D req;
            try { req = Json.Parse(line) as D; }
            catch (Exception e) { return Json.Write(Fail("Bad request: " + e.Message)); }
            if (req == null) return Json.Write(Fail("Bad request."));
            if (Str(req, "token") != token) return Json.Write(Fail("Wrong key. Re-read the bridge file."));
            var p = new Pending { req = req };
            lock (qlock) queue.Enqueue(p);
            int wait = (int)(Num(req, "timeout", 60) * 1000);
            if (!p.done.WaitOne(wait))
            {
                lock (qlock)
                {
                    if (p.resp == null)
                    {
                        p.cancelled = true;   // never run it later, after the agent gave up on it
                        return Json.Write(Fail("Unity didn't answer in time. It may be busy importing, showing a " +
                                               "dialog, or slowed down because it's in the background " +
                                               "(Preferences > General > Interaction Mode > No Throttling fixes that)."));
                    }
                }
            }
            return p.resp;
        }

        static void Pump()
        {
            for (int i = 0; i < 8; i++)
            {
                Pending p;
                lock (qlock)
                {
                    if (queue.Count == 0) return;
                    p = queue.Dequeue();
                    if (p.cancelled) continue;
                }
                object result;
                try { result = Execute(p.req); }
                catch (Exception e) { result = Fail(e.GetType().Name + ": " + e.Message); }
                string text;
                try { text = Json.Write(result); }
                catch (Exception e) { text = Json.Write(Fail("Couldn't encode the answer: " + e.Message)); }
                lock (qlock) p.resp = text;
                p.done.Set();
            }
        }

        static D Ok(object result) { return new D { {"ok", true}, {"result", result} }; }
        static D Fail(string error) { return new D { {"ok", false}, {"error", error} }; }

        class BridgeError : Exception { public BridgeError(string m) : base(m) { } }

        static object Execute(D r)
        {
            switch (Str(r, "cmd"))
            {
                case "ping": return Ok(Ping());
                case "hierarchy": return Ok(Hierarchy(r));
                case "inspect": return Ok(Inspect(r));
                case "find": return Ok(Find(r));
                case "assets": return Ok(Assets(r));
                case "create": return Ok(Create(r));
                case "create_asset": return Ok(CreateAsset(r));
                case "modify": return Ok(Modify(r));
                case "delete": return Ok(Delete(r));
                case "refresh": return Ok(Refresh());
                case "compile": return Ok(CompileReport());
                case "console": return Ok(Console(r));
                case "play": return Ok(Play(r));
                case "screenshot": return Ok(Screenshot(r));
                case "scene": return Ok(SceneCmd(r));
                case "menu": return Ok(Menu(r));
                case "undo": return Ok(UndoCmd(r));
                case "save_prefab": return Ok(SavePrefab(r));
                case "select": return Ok(Select(r));
                default: return Fail("Unknown command '" + Str(r, "cmd") + "'.");
            }
        }

        // ------------------------------------------------------------------ status
        static D Ping()
        {
            var scenes = new L();
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                var s = SceneManager.GetSceneAt(i);
                scenes.Add(new D { {"name", s.name}, {"path", s.path}, {"dirty", s.isDirty}, {"loaded", s.isLoaded},
                                   {"active", s == SceneManager.GetActiveScene()} });
            }
            var sel = new L();
            foreach (var go in Selection.gameObjects.Take(10)) sel.Add(PathOf(go));
            if (Selection.activeObject != null && !(Selection.activeObject is GameObject))
            {
                string ap = AssetDatabase.GetAssetPath(Selection.activeObject);
                if (ap != "") sel.Add(ap);
            }
            var rp = GraphicsSettings.currentRenderPipeline;
            return new D
            {
                {"unity", Application.unityVersion}, {"project", projectRoot}, {"product", PlayerSettings.productName},
                {"bridge", BridgeVersion}, {"playing", EditorApplication.isPlaying}, {"paused", EditorApplication.isPaused},
                {"compiling", EditorApplication.isCompiling}, {"updating", EditorApplication.isUpdating},
                {"compile_count", SessionState.GetInt("LocalAgent.compileCount", 0)},
                {"compile_errors", compileMsgs.Count(m => (string)m["type"] == "error")},
                {"scenes", scenes}, {"selection", sel},
                {"render_pipeline", rp == null ? "Built-in" : rp.GetType().Name},
            };
        }

        // ------------------------------------------------------------------ finding objects
        public static string PathOf(GameObject go)
        {
            var parts = new List<string>();
            for (var t = go.transform; t != null; t = t.parent) parts.Add(t.name);
            parts.Reverse();
            return string.Join("/", parts.ToArray());
        }

        static IEnumerable<GameObject> AllObjects()
        {
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                var s = SceneManager.GetSceneAt(i);
                if (!s.isLoaded) continue;
                foreach (var root in s.GetRootGameObjects())
                    foreach (var t in root.GetComponentsInChildren<Transform>(true))
                        yield return t.gameObject;
            }
        }

        /// <summary>An object to work on: a scene object, or one inside a prefab asset (opened for editing).</summary>
        class Target : IDisposable
        {
            public GameObject go;
            public string prefabPath;
            public GameObject prefabRoot;
            public bool changed;

            public string Path
            {
                get
                {
                    if (prefabRoot == null) return PathOf(go);
                    string rel = PathOf(go);
                    int cut = rel.IndexOf('/');
                    return cut < 0 ? prefabPath : prefabPath + rel.Substring(cut);
                }
            }

            public void Dirty()
            {
                changed = true;
                if (prefabRoot == null && !EditorApplication.isPlaying) EditorSceneManager.MarkSceneDirty(go.scene);
            }

            public void Dispose()
            {
                if (prefabRoot == null) return;
                if (changed) PrefabUtility.SaveAsPrefabAsset(prefabRoot, prefabPath);
                PrefabUtility.UnloadPrefabContents(prefabRoot);
                prefabRoot = null;
            }
        }

        static bool IsAssetPath(string p)
        {
            return p.StartsWith("Assets/", StringComparison.OrdinalIgnoreCase) ||
                   p.StartsWith("Packages/", StringComparison.OrdinalIgnoreCase);
        }

        static Target Resolve(string path)
        {
            if (string.IsNullOrEmpty(path)) throw new BridgeError("Give the object's path, e.g. 'Player/Camera', or its #id.");
            path = path.Trim();
            int pi = path.IndexOf(".prefab", StringComparison.OrdinalIgnoreCase);
            if (IsAssetPath(path) && pi > 0)
            {
                string asset = path.Substring(0, pi + 7);
                string rest = path.Substring(pi + 7).Trim('/');
                if (AssetDatabase.LoadAssetAtPath<GameObject>(asset) == null) throw new BridgeError("No prefab at " + asset);
                var root = PrefabUtility.LoadPrefabContents(asset);
                var t = new Target { prefabPath = asset, prefabRoot = root, go = root };
                if (rest != "")
                {
                    var child = root.transform.Find(rest);
                    if (child == null)
                    {
                        t.Dispose();
                        throw new BridgeError("The prefab " + asset + " has no child '" + rest + "'.");
                    }
                    t.go = child.gameObject;
                }
                return t;
            }
            return new Target { go = FindObject(path) };
        }

        static GameObject FindObject(string path)
        {
            if (path.StartsWith("#"))
            {
                int id;
                if (!int.TryParse(path.Substring(1), out id)) throw new BridgeError("Bad id " + path);
                var o = EditorUtility.InstanceIDToObject(id);
                if (o is GameObject) return (GameObject)o;
                if (o is Component) return ((Component)o).gameObject;
                throw new BridgeError("Nothing with id " + path + " (ids change when Unity reloads; look it up again).");
            }
            path = path.Trim('/');
            var hits = AllObjects().Where(g => PathOf(g) == path).ToList();
            if (hits.Count == 0)
            {
                // a unique name anywhere is fine too
                string name = path.Contains("/") ? null : path;
                if (name != null) hits = AllObjects().Where(g => g.name == name).ToList();
            }
            if (hits.Count == 1) return hits[0];
            if (hits.Count > 1)
                throw new BridgeError(hits.Count + " objects match '" + path + "': " +
                                      string.Join(", ", hits.Take(8).Select(g => "#" + g.GetInstanceID()).ToArray()) +
                                      ". Use the #id.");
            throw new BridgeError("No object '" + path + "' in the open scenes. Use unity_hierarchy or unity_find to look.");
        }

        static Type FindType(string name, Type baseType)
        {
            if (string.IsNullOrEmpty(name)) return null;
            Type loose = null;
            foreach (var t in TypeCache.GetTypesDerivedFrom(baseType).Concat(new[] { baseType }))
            {
                if (t.Name == name || t.FullName == name) return t;
                if (loose == null && (string.Equals(t.Name, name, StringComparison.OrdinalIgnoreCase) ||
                                      string.Equals(t.FullName, name, StringComparison.OrdinalIgnoreCase)))
                    loose = t;
            }
            return loose;
        }

        static Component FindComponent(GameObject go, string name, bool required = true)
        {
            var comps = go.GetComponents<Component>().Where(c => c != null).ToArray();
            var c1 = comps.FirstOrDefault(c => c.GetType().Name == name) ??
                     comps.FirstOrDefault(c => string.Equals(c.GetType().Name, name, StringComparison.OrdinalIgnoreCase));
            if (c1 == null)
            {
                var t = FindType(name, typeof(Component));
                if (t != null) c1 = comps.FirstOrDefault(c => t.IsAssignableFrom(c.GetType()));
            }
            if (c1 == null && required)
                throw new BridgeError("'" + go.name + "' has no " + name + ". It has: " +
                                      string.Join(", ", comps.Select(c => c.GetType().Name).ToArray()));
            return c1;
        }

        // ------------------------------------------------------------------ hierarchy and find
        static object Hierarchy(D r)
        {
            int depth = (int)Num(r, "depth", 3);
            bool comps = Bool(r, "components", false);
            int limit = (int)Num(r, "limit", 400);
            int count = 0;
            bool cut = false;
            Func<GameObject, int, D> node = null;
            node = (go, d) =>
            {
                count++;
                var n = new D { {"name", go.name}, {"id", go.GetInstanceID()} };
                if (!go.activeSelf) n["active"] = false;
                if (comps)
                    n["components"] = new L(go.GetComponents<Component>()
                                              .Select(c => (object)(c == null ? "Missing script" : c.GetType().Name))
                                              .Where(s => (string)s != "Transform"));
                int kids = go.transform.childCount;
                if (kids > 0)
                {
                    if (d >= depth || count >= limit)
                    {
                        n["more"] = kids;
                        if (count >= limit) cut = true;
                    }
                    else
                    {
                        var list = new L();
                        foreach (Transform c in go.transform)
                        {
                            if (count >= limit) { cut = true; n["more"] = kids - list.Count; break; }
                            list.Add(node(c.gameObject, d + 1));
                        }
                        n["children"] = list;
                    }
                }
                return n;
            };

            string under = Str(r, "under");
            if (!string.IsNullOrEmpty(under))
            {
                using (var t = Resolve(under))
                {
                    var n = node(t.go, 0);
                    n["path"] = t.Path;
                    return new D { {"roots", new L { n }}, {"truncated", cut} };
                }
            }
            string only = Str(r, "scene");
            var scenes = new L();
            for (int i = 0; i < SceneManager.sceneCount; i++)
            {
                var s = SceneManager.GetSceneAt(i);
                if (!string.IsNullOrEmpty(only) && !string.Equals(s.name, only, StringComparison.OrdinalIgnoreCase)) continue;
                var roots = new L();
                if (s.isLoaded)
                    foreach (var go in s.GetRootGameObjects())
                    {
                        if (count >= limit) { cut = true; break; }
                        roots.Add(node(go, 0));
                    }
                scenes.Add(new D { {"name", s.name}, {"path", s.path}, {"dirty", s.isDirty}, {"roots", roots} });
            }
            return new D { {"scenes", scenes}, {"truncated", cut} };
        }

        static object Find(D r)
        {
            string name = Str(r, "name");
            string comp = Str(r, "component");
            string tag = Str(r, "tag");
            int limit = (int)Num(r, "limit", 100);
            Type ct = null;
            if (!string.IsNullOrEmpty(comp))
            {
                ct = FindType(comp, typeof(Component));
                if (ct == null) throw new BridgeError("No component type called '" + comp + "'.");
            }
            var hits = new L();
            int total = 0;
            foreach (var go in AllObjects())
            {
                if (!string.IsNullOrEmpty(name) && go.name.IndexOf(name, StringComparison.OrdinalIgnoreCase) < 0) continue;
                if (ct != null && go.GetComponent(ct) == null) continue;
                if (!string.IsNullOrEmpty(tag) && !go.CompareTag(tag)) continue;
                total++;
                if (hits.Count < limit)
                    hits.Add(new D { {"path", PathOf(go)}, {"id", go.GetInstanceID()}, {"active", go.activeInHierarchy} });
            }
            return new D { {"objects", hits}, {"total", total} };
        }

        static object Assets(D r)
        {
            string filter = Str(r, "filter") ?? "";
            string folder = Str(r, "folder");
            int limit = (int)Num(r, "limit", 100);
            string[] guids = string.IsNullOrEmpty(folder)
                ? AssetDatabase.FindAssets(filter)
                : AssetDatabase.FindAssets(filter, new[] { folder.TrimEnd('/') });
            var list = new L();
            foreach (var g in guids.Take(limit))
            {
                string p = AssetDatabase.GUIDToAssetPath(g);
                var t = AssetDatabase.GetMainAssetTypeAtPath(p);
                list.Add(new D { {"path", p}, {"type", t == null ? "?" : t.Name} });
            }
            return new D { {"assets", list}, {"total", guids.Length} };
        }

        // ------------------------------------------------------------------ inspect
        static object Inspect(D r)
        {
            string path = Str(r, "path");
            string only = Str(r, "component");
            int max = (int)Num(r, "max_props", 60);
            if (path != null && IsAssetPath(path) && path.IndexOf(".prefab", StringComparison.OrdinalIgnoreCase) < 0)
                return InspectAsset(path, max);
            using (var t = Resolve(path))
            {
                var go = t.go;
                var comps = new L();
                foreach (var c in go.GetComponents<Component>())
                {
                    if (c == null)
                    {
                        comps.Add(new D { {"type", "Missing script"} });
                        continue;
                    }
                    if (!string.IsNullOrEmpty(only) && c != FindComponent(go, only, false)) continue;
                    comps.Add(DescribeComponent(c, max));
                }
                var res = new D
                {
                    {"path", t.Path}, {"id", go.GetInstanceID()}, {"active", go.activeSelf}, {"tag", go.tag},
                    {"layer", LayerMask.LayerToName(go.layer)}, {"static", go.isStatic}, {"components", comps},
                };
                if (t.prefabRoot == null && PrefabUtility.IsPartOfPrefabInstance(go))
                    res["prefab"] = PrefabUtility.GetPrefabAssetPathOfNearestInstanceRoot(go);
                return res;
            }
        }

        static D DescribeComponent(Component c, int max)
        {
            var d = new D { {"type", c.GetType().Name} };
            var b = c as Behaviour;
            if (b != null) d["enabled"] = b.enabled;
            var rend = c as Renderer;
            if (rend != null)
            {
                d["enabled"] = rend.enabled;
                d["materials"] = new L(rend.sharedMaterials.Select(m => (object)(m == null ? "None" : m.name + " (" + AssetDatabase.GetAssetPath(m) + ")")));
            }
            var col = c as Collider;
            if (col != null) d["enabled"] = col.enabled;
            var tr = c as Transform;
            if (tr != null)
            {
                d["props"] = new L
                {
                    Prop("position", "Vector3", Vec(tr.localPosition)),
                    Prop("rotation", "Vector3", Vec(tr.localEulerAngles)),
                    Prop("scale", "Vector3", Vec(tr.localScale)),
                    Prop("world position", "Vector3", Vec(tr.position)),
                };
                return d;
            }
            d["props"] = DescribeProps(new SerializedObject(c), max);
            return d;
        }

        static D Prop(string path, string type, string value)
        {
            return new D { {"path", path}, {"type", type}, {"value", value} };
        }

        static L DescribeProps(SerializedObject so, int max)
        {
            var list = new L();
            var it = so.GetIterator();
            bool enter = true;
            while (it.NextVisible(enter))
            {
                enter = it.propertyType == SerializedPropertyType.Generic && it.depth < 2 &&
                        (!it.isArray || it.arraySize <= 8);
                if (it.propertyPath == "m_Script" && it.depth == 0)
                {
                    list.Add(Prop("script", "MonoScript", FormatValue(it)));
                    continue;
                }
                if (it.propertyType == SerializedPropertyType.Generic && !it.isArray) continue;   // its fields follow
                if (list.Count >= max)
                {
                    list.Add(Prop("...", "", "more properties not shown"));
                    break;
                }
                list.Add(Prop(it.propertyPath, PropType(it), FormatValue(it)));
            }
            return list;
        }

        static string PropType(SerializedProperty p)
        {
            switch (p.propertyType)
            {
                case SerializedPropertyType.ObjectReference:
                    return RefType(p) ?? "Object";
                case SerializedPropertyType.Enum:
                    return "enum";
                case SerializedPropertyType.Generic:
                    return p.isArray ? "list" : p.type;
                default:
                    return p.propertyType.ToString();
            }
        }

        static string RefType(SerializedProperty p)
        {
            string t = p.type;   // "PPtr<Material>" or "PPtr<$MyScript>"
            if (!t.StartsWith("PPtr<")) return null;
            return t.Substring(5).TrimEnd('>').TrimStart('$');
        }

        static string F(float f) { return f.ToString("0.####", Inv); }
        static string Vec(Vector2 v) { return "(" + F(v.x) + ", " + F(v.y) + ")"; }
        static string Vec(Vector3 v) { return "(" + F(v.x) + ", " + F(v.y) + ", " + F(v.z) + ")"; }
        static string Vec(Vector4 v) { return "(" + F(v.x) + ", " + F(v.y) + ", " + F(v.z) + ", " + F(v.w) + ")"; }
        static string Hex(Color c)
        {
            return "#" + ColorUtility.ToHtmlStringRGBA(c) + (c.maxColorComponent > 1f ? " (HDR " + Vec((Vector4)c) + ")" : "");
        }

        static string Short(string s, int n)
        {
            if (s == null) return "";
            return s.Length <= n ? s : s.Substring(0, n) + "...";
        }

        static string ObjName(Object o)
        {
            if (o == null) return "None";
            string ap = AssetDatabase.GetAssetPath(o);
            if (ap != "") return o.name + " (" + o.GetType().Name + ", " + ap + ")";
            var go = o is Component ? ((Component)o).gameObject : o as GameObject;
            if (go != null) return o.name + " (" + o.GetType().Name + " on " + PathOf(go) + ")";
            return o.name + " (" + o.GetType().Name + ")";
        }

        static string FormatValue(SerializedProperty p)
        {
            switch (p.propertyType)
            {
                case SerializedPropertyType.Integer: return p.longValue.ToString(Inv);
                case SerializedPropertyType.Boolean: return p.boolValue ? "true" : "false";
                case SerializedPropertyType.Float: return p.doubleValue.ToString("0.####", Inv);
                case SerializedPropertyType.String: return "\"" + Short(p.stringValue, 200) + "\"";
                case SerializedPropertyType.Color: return Hex(p.colorValue);
                case SerializedPropertyType.ObjectReference: return ObjName(p.objectReferenceValue);
                case SerializedPropertyType.LayerMask: return LayerNames(p.intValue);
                case SerializedPropertyType.Enum:
                    return p.enumValueIndex >= 0 && p.enumValueIndex < p.enumNames.Length ? p.enumNames[p.enumValueIndex] : p.intValue.ToString(Inv);
                case SerializedPropertyType.Vector2: return Vec(p.vector2Value);
                case SerializedPropertyType.Vector3: return Vec(p.vector3Value);
                case SerializedPropertyType.Vector4: return Vec(p.vector4Value);
                case SerializedPropertyType.Vector2Int: return p.vector2IntValue.ToString();
                case SerializedPropertyType.Vector3Int: return p.vector3IntValue.ToString();
                case SerializedPropertyType.Quaternion: return Vec(p.quaternionValue.eulerAngles) + " (euler)";
                case SerializedPropertyType.Rect: return p.rectValue.ToString();
                case SerializedPropertyType.Bounds: return p.boundsValue.ToString();
                case SerializedPropertyType.ArraySize: return p.intValue.ToString(Inv);
                case SerializedPropertyType.Character: return "'" + (char)p.intValue + "'";
                case SerializedPropertyType.AnimationCurve: return "curve with " + p.animationCurveValue.length + " keys";
                case SerializedPropertyType.Gradient: return "gradient";
                case SerializedPropertyType.ManagedReference: return p.managedReferenceFullTypename;
                case SerializedPropertyType.Generic: return p.isArray ? p.arraySize + " items" : p.type;
                default: return p.propertyType.ToString();
            }
        }

        static string LayerNames(int mask)
        {
            if (mask == 0) return "Nothing";
            if (mask == -1) return "Everything";
            var names = new List<string>();
            for (int i = 0; i < 32; i++)
                if ((mask & (1 << i)) != 0) names.Add(LayerMask.LayerToName(i) == "" ? "Layer " + i : LayerMask.LayerToName(i));
            return string.Join(", ", names.ToArray());
        }

        static object InspectAsset(string path, int max)
        {
            var main = AssetDatabase.LoadMainAssetAtPath(path);
            if (main == null) throw new BridgeError("No asset at " + path);
            var res = new D { {"path", path}, {"type", main.GetType().Name} };
            var mat = main as Material;
            if (mat != null)
            {
                res["shader"] = mat.shader == null ? "None" : mat.shader.name;
                res["props"] = MaterialProps(mat, max);
                return res;
            }
            var mono = main as MonoScript;
            if (mono != null)
            {
                var cls = mono.GetClass();
                res["class"] = cls == null ? "None (file name and class name must match)" : cls.FullName;
                return res;
            }
            res["props"] = DescribeProps(new SerializedObject(main), max);
            return res;
        }

        static L MaterialProps(Material mat, int max)
        {
            var list = new L();
            var sh = mat.shader;
            if (sh == null) return list;
            int n = sh.GetPropertyCount();
            for (int i = 0; i < n && list.Count < max; i++)
            {
                if ((sh.GetPropertyFlags(i) & ShaderPropertyFlags.HideInInspector) != 0) continue;
                string name = sh.GetPropertyName(i);
                string val;
                switch (sh.GetPropertyType(i))
                {
                    case ShaderPropertyType.Color: val = Hex(mat.GetColor(name)); break;
                    case ShaderPropertyType.Vector: val = Vec(mat.GetVector(name)); break;
                    case ShaderPropertyType.Texture: val = ObjName(mat.GetTexture(name)); break;
                    default: val = F(mat.GetFloat(name)); break;
                }
                list.Add(Prop(name, sh.GetPropertyType(i).ToString(), val));
            }
            return list;
        }

        // ------------------------------------------------------------------ changing things
        static object Create(D r)
        {
            if (EditorApplication.isPlaying && !Bool(r, "in_play_mode", false))
                throw new BridgeError("Unity is in play mode, where changes are lost when it stops. Stop play mode first.");
            string prefab = Str(r, "prefab");
            string prim = (Str(r, "primitive") ?? "empty").ToLowerInvariant();
            string parent = Str(r, "parent");
            Target pt = string.IsNullOrEmpty(parent) ? null : Resolve(parent);
            try
            {
                GameObject go;
                if (!string.IsNullOrEmpty(prefab))
                {
                    var asset = AssetDatabase.LoadAssetAtPath<GameObject>(prefab);
                    if (asset == null) throw new BridgeError("No prefab at " + prefab);
                    go = (GameObject)PrefabUtility.InstantiatePrefab(asset);
                }
                else if (prim == "empty" || prim == "")
                    go = new GameObject(Str(r, "name") ?? "GameObject");
                else
                {
                    PrimitiveType pt2;
                    if (!TryEnum(prim, out pt2)) throw new BridgeError("Primitive must be empty, cube, sphere, capsule, cylinder, plane or quad.");
                    go = GameObject.CreatePrimitive(pt2);
                }
                if (pt != null) go.transform.SetParent(pt.go.transform, false);
                else
                {
                    string scene = Str(r, "scene");
                    if (!string.IsNullOrEmpty(scene))
                    {
                        var s = SceneManager.GetSceneByName(scene);
                        if (!s.IsValid()) throw new BridgeError("No open scene called " + scene);
                        SceneManager.MoveGameObjectToScene(go, s);
                    }
                }
                if (Str(r, "name") != null) go.name = Str(r, "name");
                var notes = new L();
                foreach (var cn in List(r, "components"))
                {
                    var t = FindType(Convert.ToString(cn, Inv), typeof(Component));
                    if (t == null) notes.Add("No component type '" + cn + "' (if it's your script, run unity_refresh first).");
                    else if (go.GetComponent(t) == null || t == typeof(Transform)) go.AddComponent(t);
                }
                var target = pt != null && pt.prefabRoot != null
                    ? new Target { go = go, prefabRoot = pt.prefabRoot, prefabPath = pt.prefabPath }
                    : new Target { go = go };
                ApplyChanges(target, r, notes);
                if (pt == null || pt.prefabRoot == null) Undo.RegisterCreatedObjectUndo(go, "Create " + go.name);
                if (pt != null) pt.changed = true;
                else target.Dirty();
                return new D { {"path", target.Path}, {"id", go.GetInstanceID()}, {"notes", notes} };
            }
            finally
            {
                if (pt != null) pt.Dispose();
            }
        }

        static object Modify(D r)
        {
            string path = Str(r, "path");
            if (EditorApplication.isPlaying && !Bool(r, "in_play_mode", false))
                throw new BridgeError("Unity is in play mode, where changes are lost when it stops. Stop play mode " +
                                      "first, or pass in_play_mode=true to try something out on purpose.");
            if (path != null && IsAssetPath(path) && path.IndexOf(".prefab", StringComparison.OrdinalIgnoreCase) < 0)
                return ModifyAsset(path, r);
            using (var t = Resolve(path))
            {
                var notes = new L();
                ApplyChanges(t, r, notes);
                t.Dirty();
                return new D { {"path", t.Path}, {"id", t.go.GetInstanceID()}, {"notes", notes} };
            }
        }

        static void ApplyChanges(Target t, D r, L notes)
        {
            var go = t.go;
            bool undo = t.prefabRoot == null;
            if (r.ContainsKey("name") && Str(r, "cmd") == "modify")
            {
                if (undo) Undo.RecordObject(go, "Rename");
                go.name = Str(r, "name");
            }
            if (r.ContainsKey("active"))
            {
                if (undo) Undo.RecordObject(go, "Set active");
                go.SetActive(ToBool(r["active"]));
            }
            if (r.ContainsKey("tag"))
            {
                try { if (undo) Undo.RecordObject(go, "Tag"); go.tag = Str(r, "tag"); }
                catch (UnityException) { notes.Add("No tag '" + Str(r, "tag") + "'. Add it in Project Settings > Tags and Layers."); }
            }
            if (r.ContainsKey("layer"))
            {
                object lv = r["layer"];
                int layer;
                if (!(lv is string) || !int.TryParse((string)lv, out layer))
                    layer = lv is string ? LayerMask.NameToLayer((string)lv) : (int)ToD(lv);
                if (layer < 0 || layer > 31) notes.Add("No layer '" + lv + "'.");
                else { if (undo) Undo.RecordObject(go, "Layer"); go.layer = layer; }
            }
            if (r.ContainsKey("parent") && Str(r, "cmd") == "modify")
            {
                string pp = Str(r, "parent");
                if (t.prefabRoot != null) notes.Add("Moving objects inside a prefab asset isn't supported; open the scene copy instead.");
                else if (string.IsNullOrEmpty(pp) || pp == "/")
                {
                    if (undo) Undo.SetTransformParent(go.transform, null, "Unparent"); else go.transform.SetParent(null, true);
                }
                else
                {
                    var p = FindObject(pp);
                    if (p.transform.IsChildOf(go.transform)) notes.Add("Can't put an object inside its own child.");
                    else Undo.SetTransformParent(go.transform, p.transform, "Reparent");
                }
            }
            var tr = go.transform;
            bool world = Bool(r, "world", false);
            if (r.ContainsKey("position") || r.ContainsKey("rotation") || r.ContainsKey("scale"))
            {
                if (undo) Undo.RecordObject(tr, "Move");
                if (r.ContainsKey("position"))
                {
                    var v = ToVector3(r["position"]);
                    if (world) tr.position = v; else tr.localPosition = v;
                }
                if (r.ContainsKey("rotation"))
                {
                    var v = ToVector3(r["rotation"]);
                    if (world) tr.eulerAngles = v; else tr.localEulerAngles = v;
                }
                if (r.ContainsKey("scale"))
                {
                    object sv = r["scale"];
                    tr.localScale = sv is double || sv is long || sv is string && !((string)sv).Contains(",")
                        ? Vector3.one * (float)ToD(sv) : ToVector3(sv);
                }
            }
            if (Str(r, "cmd") == "modify")
            {
                foreach (var cn in List(r, "add_components"))
                {
                    var ty = FindType(Convert.ToString(cn, Inv), typeof(Component));
                    if (ty == null) notes.Add("No component type '" + cn + "' (if it's your script, run unity_refresh and fix compile errors first).");
                    else if (undo) Undo.AddComponent(go, ty); else go.AddComponent(ty);
                }
                foreach (var cn in List(r, "remove_components"))
                {
                    var c = FindComponent(go, Convert.ToString(cn, Inv), false);
                    if (c == null || c is Transform) notes.Add("No removable component '" + cn + "'.");
                    else if (undo) Undo.DestroyObjectImmediate(c); else Object.DestroyImmediate(c);
                }
            }
            var set = r.ContainsKey("set") ? r["set"] as D : null;
            if (set != null)
                foreach (var kv in set)
                {
                    string key = kv.Key;
                    int dot = key.IndexOf('.');
                    if (dot <= 0) { notes.Add("'" + key + "': use Component.property, e.g. Rigidbody.mass."); continue; }
                    string cname = key.Substring(0, dot), prop = key.Substring(dot + 1);
                    if (cname == "GameObject") { notes.Add("Use the name, active, tag and layer settings for the GameObject itself."); continue; }
                    var comp = FindComponent(go, cname, false);
                    if (comp == null) { notes.Add("'" + go.name + "' has no " + cname + "."); continue; }
                    if (comp is Behaviour && prop == "enabled") { if (undo) Undo.RecordObject(comp, "Enable"); ((Behaviour)comp).enabled = ToBool(kv.Value); continue; }
                    var so = new SerializedObject(comp);
                    var sp = FindProp(so, prop);
                    if (sp == null) { notes.Add(cname + " has no property '" + prop + "'. Use unity_inspect to see them."); continue; }
                    string err = SetValue(sp, kv.Value);
                    if (err != null) { notes.Add(key + ": " + err); continue; }
                    if (undo) so.ApplyModifiedProperties(); else so.ApplyModifiedPropertiesWithoutUndo();
                    notes.Add(key + " = " + FormatValue(so.FindProperty(sp.propertyPath)));
                }
        }

        static object ModifyAsset(string path, D r)
        {
            var main = AssetDatabase.LoadMainAssetAtPath(path);
            if (main == null) throw new BridgeError("No asset at " + path);
            var notes = new L();
            var set = r.ContainsKey("set") ? r["set"] as D : new D();
            var mat = main as Material;
            Undo.RecordObject(main, "Change " + main.name);
            if (mat != null)
            {
                foreach (var kv in set) notes.Add(SetMaterial(mat, kv.Key, kv.Value));
            }
            else
            {
                var so = new SerializedObject(main);
                foreach (var kv in set)
                {
                    var sp = FindProp(so, kv.Key);
                    if (sp == null) { notes.Add("No property '" + kv.Key + "'."); continue; }
                    string err = SetValue(sp, kv.Value);
                    notes.Add(err != null ? kv.Key + ": " + err : kv.Key + " = " + FormatValue(sp));
                }
                so.ApplyModifiedProperties();
            }
            EditorUtility.SetDirty(main);
            AssetDatabase.SaveAssets();
            return new D { {"path", path}, {"notes", notes} };
        }

        static string SetMaterial(Material mat, string key, object v)
        {
            string k = key.TrimStart('_').ToLowerInvariant();
            if (k == "shader")
            {
                var sh = Shader.Find(Convert.ToString(v, Inv));
                if (sh == null) return "No shader called '" + v + "'.";
                mat.shader = sh;
                return "shader = " + sh.name;
            }
            if (k == "color" && !HasProp(mat, "_Color") || k == "maincolor")
            {
                mat.color = ToColor(v);
                return "main color = " + Hex(mat.color);
            }
            var shd = mat.shader;
            for (int i = 0; i < shd.GetPropertyCount(); i++)
            {
                string name = shd.GetPropertyName(i);
                if (name.TrimStart('_').ToLowerInvariant() != k) continue;
                switch (shd.GetPropertyType(i))
                {
                    case ShaderPropertyType.Color: mat.SetColor(name, ToColor(v)); return name + " = " + Hex(mat.GetColor(name));
                    case ShaderPropertyType.Vector: mat.SetVector(name, ToVector4(v)); return name + " = " + Vec(mat.GetVector(name));
                    case ShaderPropertyType.Texture:
                        var tex = v == null || Convert.ToString(v, Inv) == "" ? null : AssetDatabase.LoadAssetAtPath<Texture>(Convert.ToString(v, Inv));
                        if (v != null && Convert.ToString(v, Inv) != "" && tex == null) return "No texture at " + v;
                        mat.SetTexture(name, tex);
                        return name + " = " + ObjName(tex);
                    default: mat.SetFloat(name, (float)ToD(v)); return name + " = " + F(mat.GetFloat(name));
                }
            }
            return "The shader " + shd.name + " has no property '" + key + "'. Use unity_inspect on the material.";
        }

        static bool HasProp(Material m, string name) { return m.HasProperty(name); }

        static SerializedProperty FindProp(SerializedObject so, string name)
        {
            var p = so.FindProperty(name);
            if (p != null) return p;
            if (!name.StartsWith("m_") && name.Length > 0)
            {
                p = so.FindProperty("m_" + char.ToUpperInvariant(name[0]) + name.Substring(1));
                if (p != null) return p;
            }
            string want = Norm(name);
            var it = so.GetIterator();
            int seen = 0;
            bool enter = true;
            while (it.Next(enter) && seen++ < 3000)
            {
                enter = it.depth < 1 && it.propertyType == SerializedPropertyType.Generic && !it.isArray;
                if (Norm(it.name) == want || Norm(it.displayName) == want) return it.Copy();
            }
            return null;
        }

        static string Norm(string s)
        {
            if (s.StartsWith("m_")) s = s.Substring(2);
            return s.Replace(" ", "").Replace("_", "").ToLowerInvariant();
        }

        static string SetValue(SerializedProperty p, object v)
        {
            try
            {
                switch (p.propertyType)
                {
                    case SerializedPropertyType.Integer: p.longValue = (long)Math.Round(ToD(v)); return null;
                    case SerializedPropertyType.ArraySize: p.intValue = (int)ToD(v); return null;
                    case SerializedPropertyType.Character:
                        p.intValue = v is string && ((string)v).Length > 0 ? ((string)v)[0] : (int)ToD(v); return null;
                    case SerializedPropertyType.Boolean: p.boolValue = ToBool(v); return null;
                    case SerializedPropertyType.Float: p.doubleValue = ToD(v); return null;
                    case SerializedPropertyType.String: p.stringValue = v == null ? "" : Convert.ToString(v, Inv); return null;
                    case SerializedPropertyType.Color: p.colorValue = ToColor(v); return null;
                    case SerializedPropertyType.Vector2: { var f = Floats(v, 2); p.vector2Value = new Vector2(f[0], f[1]); return null; }
                    case SerializedPropertyType.Vector3: p.vector3Value = ToVector3(v); return null;
                    case SerializedPropertyType.Vector4: p.vector4Value = ToVector4(v); return null;
                    case SerializedPropertyType.Vector2Int: { var f = Floats(v, 2); p.vector2IntValue = new Vector2Int((int)f[0], (int)f[1]); return null; }
                    case SerializedPropertyType.Vector3Int: { var f = Floats(v, 3); p.vector3IntValue = new Vector3Int((int)f[0], (int)f[1], (int)f[2]); return null; }
                    case SerializedPropertyType.Rect: { var f = Floats(v, 4); p.rectValue = new Rect(f[0], f[1], f[2], f[3]); return null; }
                    case SerializedPropertyType.Quaternion:
                        {
                            var f = Floats(v, 3);
                            p.quaternionValue = f.Length >= 4 ? new Quaternion(f[0], f[1], f[2], f[3]) : Quaternion.Euler(f[0], f[1], f[2]);
                            return null;
                        }
                    case SerializedPropertyType.Enum:
                        {
                            if (v is string)
                            {
                                string s = ((string)v).Replace(" ", "");
                                for (int i = 0; i < p.enumNames.Length; i++)
                                    if (string.Equals(p.enumNames[i].Replace(" ", ""), s, StringComparison.OrdinalIgnoreCase) ||
                                        string.Equals(p.enumDisplayNames[i].Replace(" ", ""), s, StringComparison.OrdinalIgnoreCase))
                                    {
                                        p.enumValueIndex = i;
                                        return null;
                                    }
                                return "must be one of: " + string.Join(", ", p.enumNames);
                            }
                            p.intValue = (int)ToD(v);
                            return null;
                        }
                    case SerializedPropertyType.LayerMask:
                        {
                            if (v is double || v is long) { p.intValue = (int)ToD(v); return null; }
                            var names = v is L ? ((L)v).Select(x => Convert.ToString(x, Inv)).ToArray()
                                               : Convert.ToString(v, Inv).Split(',').Select(x => x.Trim()).ToArray();
                            if (names.Length == 1 && names[0].ToLowerInvariant() == "everything") { p.intValue = -1; return null; }
                            if (names.Length == 1 && names[0].ToLowerInvariant() == "nothing") { p.intValue = 0; return null; }
                            p.intValue = LayerMask.GetMask(names);
                            return null;
                        }
                    case SerializedPropertyType.ObjectReference: return SetObjectRef(p, v);
                    case SerializedPropertyType.Generic:
                        if (p.isArray && v is L)
                        {
                            var list = (L)v;
                            p.arraySize = list.Count;
                            for (int i = 0; i < list.Count; i++)
                            {
                                string err = SetValue(p.GetArrayElementAtIndex(i), list[i]);
                                if (err != null) return "item " + i + ": " + err;
                            }
                            return null;
                        }
                        if (!p.isArray && v is D)
                        {
                            foreach (var kv in (D)v)
                            {
                                var c = p.FindPropertyRelative(kv.Key);
                                if (c == null && kv.Key.Length > 0)
                                    c = p.FindPropertyRelative("m_" + char.ToUpperInvariant(kv.Key[0]) + kv.Key.Substring(1));
                                if (c == null) return "no field '" + kv.Key + "'";
                                string err = SetValue(c, kv.Value);
                                if (err != null) return kv.Key + ": " + err;
                            }
                            return null;
                        }
                        return p.isArray ? "give a list of values" : "give an object like {\"field\": value}";
                    default:
                        return "can't set " + p.propertyType + " values";
                }
            }
            catch (BridgeError e) { return e.Message; }
            catch (FormatException e) { return e.Message; }
            catch (InvalidCastException e) { return e.Message; }
        }

        static string SetObjectRef(SerializedProperty p, object v)
        {
            string s = v == null ? "" : Convert.ToString(v, Inv).Trim();
            if (s == "" || s.ToLowerInvariant() == "none" || s.ToLowerInvariant() == "null")
            {
                p.objectReferenceValue = null;
                return null;
            }
            var cands = new List<Object>();
            if (s.StartsWith("#") || !IsAssetPath(s))
            {
                var go = FindObject(s);
                cands.Add(go);
                cands.AddRange(go.GetComponents<Component>().Where(c => c != null).Cast<Object>());
            }
            else
            {
                var main = AssetDatabase.LoadMainAssetAtPath(s);
                if (main == null) return "no asset at " + s;
                cands.Add(main);
                cands.AddRange(AssetDatabase.LoadAllAssetsAtPath(s).Where(o => o != null && o != main));
                var mono = main as MonoScript;
                if (mono != null) return "that's a script file; give an object that has the script as a component";
            }
            string want = RefType(p);
            foreach (var c in cands)
            {
                if (want == null || want == "Object" || IsType(c.GetType(), want))
                {
                    p.objectReferenceValue = c;
                    return null;
                }
            }
            return "nothing there is a " + want + " (found: " + string.Join(", ", cands.Take(6).Select(c => c.GetType().Name).ToArray()) + ")";
        }

        static bool IsType(Type t, string name)
        {
            for (; t != null; t = t.BaseType) if (t.Name == name) return true;
            return false;
        }

        static object Delete(D r)
        {
            if (EditorApplication.isPlaying) throw new BridgeError("Stop play mode first.");
            var paths = r.ContainsKey("paths") ? List(r, "paths").Select(x => Convert.ToString(x, Inv)).ToList()
                                               : new List<string> { Str(r, "path") };
            var gone = new L();
            foreach (var path in paths)
            {
                using (var t = Resolve(path))
                {
                    string shown = t.Path;
                    if (t.prefabRoot != null)
                    {
                        if (t.go == t.prefabRoot) throw new BridgeError("To delete a whole prefab asset, delete the file.");
                        Object.DestroyImmediate(t.go);
                        t.changed = true;
                    }
                    else
                    {
                        var scene = t.go.scene;
                        Undo.DestroyObjectImmediate(t.go);
                        EditorSceneManager.MarkSceneDirty(scene);
                    }
                    gone.Add(shown);
                }
            }
            return new D { {"deleted", gone} };
        }

        static object CreateAsset(D r)
        {
            string kind = (Str(r, "kind") ?? "").ToLowerInvariant();
            string path = Str(r, "path");
            if (string.IsNullOrEmpty(path) || !path.StartsWith("Assets", StringComparison.OrdinalIgnoreCase))
                throw new BridgeError("The path must be inside Assets/, e.g. Assets/Materials/Red.mat");
            if (kind == "folder")
            {
                EnsureFolder(path);
                return new D { {"path", path} };
            }
            EnsureFolder(Path.GetDirectoryName(path).Replace('\\', '/'));
            if (AssetDatabase.LoadMainAssetAtPath(path) != null) throw new BridgeError(path + " already exists.");
            Object asset;
            if (kind == "material")
            {
                string shader = Str(r, "shader");
                Shader sh = !string.IsNullOrEmpty(shader) ? Shader.Find(shader) : DefaultShader();
                if (sh == null) throw new BridgeError("No shader called '" + shader + "'.");
                asset = new Material(sh);
                if (!path.EndsWith(".mat")) path += ".mat";
            }
            else if (kind == "scriptable_object" || kind == "scriptableobject")
            {
                var t = FindType(Str(r, "type"), typeof(ScriptableObject));
                if (t == null) throw new BridgeError("No ScriptableObject class called '" + Str(r, "type") + "'.");
                asset = ScriptableObject.CreateInstance(t);
                if (!path.EndsWith(".asset")) path += ".asset";
            }
            else if (kind == "physics_material" || kind == "physic_material")
            {
                var t = FindType("PhysicsMaterial", typeof(Object)) ?? FindType("PhysicMaterial", typeof(Object));
                if (t == null) throw new BridgeError("This Unity has no physics material type.");
                asset = (Object)Activator.CreateInstance(t);
                if (!path.EndsWith(".physicMaterial") && !path.EndsWith(".physicsMaterial")) path += ".physicMaterial";
            }
            else throw new BridgeError("kind must be material, folder, scriptable_object or physics_material. Write scripts with write_file.");
            AssetDatabase.CreateAsset(asset, path);
            var set = r.ContainsKey("set") ? r["set"] as D : null;
            var notes = new L();
            if (set != null)
            {
                if (asset is Material) foreach (var kv in set) notes.Add(SetMaterial((Material)asset, kv.Key, kv.Value));
                else
                {
                    var so = new SerializedObject(asset);
                    foreach (var kv in set)
                    {
                        var sp = FindProp(so, kv.Key);
                        string err = sp == null ? "no such property" : SetValue(sp, kv.Value);
                        notes.Add(err != null ? kv.Key + ": " + err : kv.Key + " = " + FormatValue(sp));
                    }
                    so.ApplyModifiedPropertiesWithoutUndo();
                }
                EditorUtility.SetDirty(asset);
            }
            AssetDatabase.SaveAssets();
            return new D { {"path", path}, {"type", asset.GetType().Name}, {"notes", notes} };
        }

        static Shader DefaultShader()
        {
            var rp = GraphicsSettings.currentRenderPipeline;
            if (rp != null && rp.defaultMaterial != null) return rp.defaultMaterial.shader;
            return Shader.Find("Standard");
        }

        static void EnsureFolder(string path)
        {
            path = path.Replace('\\', '/').TrimEnd('/');
            if (path == "Assets" || AssetDatabase.IsValidFolder(path)) return;
            string parent = Path.GetDirectoryName(path).Replace('\\', '/');
            EnsureFolder(parent);
            AssetDatabase.CreateFolder(parent, Path.GetFileName(path));
        }

        static object SavePrefab(D r)
        {
            string asset = Str(r, "asset_path");
            if (string.IsNullOrEmpty(asset) || !asset.StartsWith("Assets/") || !asset.EndsWith(".prefab"))
                throw new BridgeError("asset_path must look like Assets/Prefabs/Enemy.prefab");
            var go = FindObject(Str(r, "path"));
            EnsureFolder(Path.GetDirectoryName(asset).Replace('\\', '/'));
            bool ok;
            PrefabUtility.SaveAsPrefabAssetAndConnect(go, asset, InteractionMode.UserAction, out ok);
            if (!ok) throw new BridgeError("Unity couldn't save the prefab.");
            return new D { {"asset", asset}, {"path", PathOf(go)} };
        }

        static object Select(D r)
        {
            string path = Str(r, "path");
            Object o = path != null && IsAssetPath(path) ? AssetDatabase.LoadMainAssetAtPath(path) : FindObject(path);
            if (o == null) throw new BridgeError("Nothing at " + path);
            Selection.activeObject = o;
            EditorGUIUtility.PingObject(o);
            if (o is GameObject && SceneView.lastActiveSceneView != null) SceneView.lastActiveSceneView.FrameSelected();
            return new D { {"selected", path} };
        }

        // ------------------------------------------------------------------ compiling and console
        static List<D> compileMsgs = new List<D>();

        static void RestoreCompile()
        {
            try
            {
                var l = Json.Parse(SessionState.GetString("LocalAgent.compileMsgs", "[]")) as L;
                compileMsgs = l == null ? new List<D>() : l.OfType<D>().ToList();
            }
            catch (Exception) { compileMsgs = new List<D>(); }
        }

        static void SaveCompile()
        {
            SessionState.SetString("LocalAgent.compileMsgs", Json.Write(new L(compileMsgs.Cast<object>())));
        }

        static void OnCompileStarted(object ctx)
        {
            compileMsgs.Clear();
            SaveCompile();
        }

        static void OnAssemblyCompiled(string assembly, CompilerMessage[] messages)
        {
            foreach (var m in messages)
            {
                if (compileMsgs.Count >= 200) break;
                string file = (m.file ?? "").Replace('\\', '/');
                if (file.StartsWith(projectRoot + "/")) file = file.Substring(projectRoot.Length + 1);
                compileMsgs.Add(new D
                {
                    {"type", m.type == CompilerMessageType.Error ? "error" : "warning"}, {"file", file},
                    {"line", m.line}, {"column", m.column}, {"message", m.message},
                });
            }
            SaveCompile();
        }

        static void OnCompileFinished(object ctx)
        {
            SessionState.SetInt("LocalAgent.compileCount", SessionState.GetInt("LocalAgent.compileCount", 0) + 1);
            SaveCompile();
        }

        static object Refresh()
        {
            AssetDatabase.Refresh(ImportAssetOptions.Default);
            return new D
            {
                {"compiling", EditorApplication.isCompiling}, {"updating", EditorApplication.isUpdating},
                {"compile_count", SessionState.GetInt("LocalAgent.compileCount", 0)},
            };
        }

        static object CompileReport()
        {
            return new D
            {
                {"compiling", EditorApplication.isCompiling}, {"compile_count", SessionState.GetInt("LocalAgent.compileCount", 0)},
                {"messages", new L(compileMsgs.Cast<object>())},
            };
        }

        class LogEntry
        {
            public long id;
            public string type, msg, stack;
        }

        static readonly List<LogEntry> logs = new List<LogEntry>();
        static long nextLogId = 1;
        static readonly object llock = new object();

        static void OnLog(string msg, string stack, LogType type)
        {
            if (msg != null && msg.StartsWith("[Local Agent]")) return;
            lock (llock)
            {
                logs.Add(new LogEntry
                {
                    id = nextLogId++, msg = Short(msg, 2000), stack = Short(stack, 1500),
                    type = type == LogType.Log ? "log" : type == LogType.Warning ? "warning" : type == LogType.Exception ? "exception" : "error",
                });
                if (logs.Count > 1000) logs.RemoveRange(0, logs.Count - 1000);
            }
        }

        static void SaveLogs()
        {
            lock (llock)
            {
                var keep = new L(logs.Skip(Math.Max(0, logs.Count - 300)).Select(e => (object)new D
                    { {"id", e.id}, {"type", e.type}, {"msg", e.msg}, {"stack", e.stack} }));
                SessionState.SetString("LocalAgent.logs", Json.Write(keep));
                SessionState.SetString("LocalAgent.logId", nextLogId.ToString(Inv));
            }
        }

        static void RestoreLogs()
        {
            try
            {
                long n;
                if (long.TryParse(SessionState.GetString("LocalAgent.logId", "1"), out n)) nextLogId = Math.Max(1, n);
                var l = Json.Parse(SessionState.GetString("LocalAgent.logs", "[]")) as L;
                if (l == null) return;
                lock (llock)
                    foreach (var d in l.OfType<D>())
                        logs.Add(new LogEntry { id = (long)ToD(d["id"]), type = (string)d["type"], msg = (string)d["msg"], stack = (string)d["stack"] });
            }
            catch (Exception) { }
        }

        static object Console(D r)
        {
            long since = (long)Num(r, "since", 0);
            string level = Str(r, "level") ?? "all";
            int limit = (int)Num(r, "limit", 50);
            bool stack = Bool(r, "stack", false);
            lock (llock)
            {
                var sel = logs.Where(e => e.id > since && (level == "all" ||
                                                          level == "warnings" && e.type != "log" ||
                                                          level == "errors" && (e.type == "error" || e.type == "exception"))).ToList();
                int total = sel.Count;
                var shown = sel.Skip(Math.Max(0, sel.Count - limit)).Select(e =>
                {
                    var d = new D { {"id", e.id}, {"type", e.type}, {"msg", e.msg} };
                    if (stack || e.type != "log") d["stack"] = Short(e.stack, stack ? 1500 : 400);
                    return (object)d;
                });
                var res = new D { {"entries", new L(shown)}, {"total", total}, {"last_id", nextLogId - 1} };
                if (Bool(r, "clear", false)) logs.Clear();
                return res;
            }
        }

        // ------------------------------------------------------------------ play mode, scenes, menus, undo
        static object Play(D r)
        {
            string action = (Str(r, "action") ?? "play").ToLowerInvariant();
            switch (action)
            {
                case "play":
                    if (compileMsgs.Any(m => (string)m["type"] == "error"))
                        throw new BridgeError("There are compile errors; Unity can't enter play mode until they're fixed. See unity_refresh.");
                    if (!EditorApplication.isPlaying) EditorApplication.isPlaying = true;
                    break;
                case "stop": EditorApplication.isPlaying = false; break;
                case "pause": EditorApplication.isPaused = true; break;
                case "resume": EditorApplication.isPaused = false; break;
                case "step": EditorApplication.Step(); break;
                default: throw new BridgeError("action must be play, stop, pause, resume or step.");
            }
            return new D { {"playing", EditorApplication.isPlaying}, {"paused", EditorApplication.isPaused}, {"last_log_id", nextLogId - 1} };
        }

        static void CheckSaved(D r)
        {
            if (Bool(r, "discard", false)) return;
            for (int i = 0; i < SceneManager.sceneCount; i++)
                if (SceneManager.GetSceneAt(i).isDirty)
                    throw new BridgeError("The scene '" + SceneManager.GetSceneAt(i).name + "' has unsaved changes. Save it first " +
                                          "(action=save), or pass discard=true if the user agreed to lose them.");
        }

        static object SceneCmd(D r)
        {
            string action = (Str(r, "action") ?? "").ToLowerInvariant();
            string path = Str(r, "path");
            if (action != "list" && EditorApplication.isPlaying) throw new BridgeError("Stop play mode first.");
            switch (action)
            {
                case "save":
                    {
                        var s = SceneManager.GetActiveScene();
                        if (!string.IsNullOrEmpty(path))
                        {
                            EnsureFolder(Path.GetDirectoryName(path).Replace('\\', '/'));
                            if (!EditorSceneManager.SaveScene(s, path)) throw new BridgeError("Couldn't save to " + path);
                            return new D { {"saved", path} };
                        }
                        if (string.IsNullOrEmpty(s.path)) throw new BridgeError("This scene was never saved; give a path like Assets/Scenes/Level1.unity");
                        if (!EditorSceneManager.SaveOpenScenes()) throw new BridgeError("Unity couldn't save the scenes.");
                        return new D { {"saved", s.path} };
                    }
                case "open":
                    {
                        if (string.IsNullOrEmpty(path)) throw new BridgeError("Give the scene path, e.g. Assets/Scenes/Main.unity");
                        bool additive = Bool(r, "additive", false);
                        if (!additive) CheckSaved(r);
                        var s = EditorSceneManager.OpenScene(path, additive ? OpenSceneMode.Additive : OpenSceneMode.Single);
                        return new D { {"opened", s.path} };
                    }
                case "new":
                    {
                        CheckSaved(r);
                        var s = EditorSceneManager.NewScene(Bool(r, "empty", false) ? NewSceneSetup.EmptyScene : NewSceneSetup.DefaultGameObjects,
                                                            NewSceneMode.Single);
                        if (!string.IsNullOrEmpty(path))
                        {
                            EnsureFolder(Path.GetDirectoryName(path).Replace('\\', '/'));
                            EditorSceneManager.SaveScene(s, path);
                        }
                        return new D { {"created", string.IsNullOrEmpty(path) ? "(unsaved)" : path} };
                    }
                case "list":
                    {
                        var build = new L(EditorBuildSettings.scenes.Select(s => (object)(s.path + (s.enabled ? "" : " (disabled)"))));
                        var all = new L(AssetDatabase.FindAssets("t:Scene", new[] { "Assets" }).Take(100)
                                                     .Select(g => (object)AssetDatabase.GUIDToAssetPath(g)));
                        return new D { {"build_scenes", build}, {"all_scenes", all} };
                    }
                case "add_to_build":
                    {
                        if (string.IsNullOrEmpty(path)) throw new BridgeError("Give the scene path.");
                        var list = EditorBuildSettings.scenes.ToList();
                        if (!list.Any(s => s.path == path)) list.Add(new EditorBuildSettingsScene(path, true));
                        EditorBuildSettings.scenes = list.ToArray();
                        return new D { {"build_scenes", new L(list.Select(s => (object)s.path))} };
                    }
                default:
                    throw new BridgeError("action must be save, open, new, list or add_to_build.");
            }
        }

        static object Menu(D r)
        {
            string item = Str(r, "item");
            if (string.IsNullOrEmpty(item)) throw new BridgeError("Give the menu path, e.g. 'GameObject/Light/Directional Light'.");
            if (!EditorApplication.ExecuteMenuItem(item)) throw new BridgeError("No menu item '" + item + "' (check the exact spelling).");
            return new D { {"ran", item} };
        }

        static object UndoCmd(D r)
        {
            int steps = Math.Max(1, Math.Min(20, (int)Num(r, "steps", 1)));
            bool redo = Bool(r, "redo", false);
            for (int i = 0; i < steps; i++)
            {
                if (redo) Undo.PerformRedo(); else Undo.PerformUndo();
            }
            return new D { {(redo ? "redone" : "undone"), steps} };
        }

        // ------------------------------------------------------------------ pictures
        static object Screenshot(D r)
        {
            string view = (Str(r, "view") ?? "scene").ToLowerInvariant();
            int w = Mathf.Clamp((int)Num(r, "width", 1024), 64, 2048);
            int h = Mathf.Clamp((int)Num(r, "height", 576), 64, 2048);
            var cams = new List<Camera>();
            string note = "";
            GameObject temp = null;
            if (view == "game")
            {
                cams = Camera.allCameras.Where(c => c.targetTexture == null).OrderBy(c => c.depth).ToList();
                if (cams.Count == 0) throw new BridgeError("There is no active camera in the scene.");
                note = "Rendered from " + string.Join(", ", cams.Select(c => c.name).ToArray()) +
                       ". Screen-space overlay UI isn't included.";
            }
            else
            {
                var sv = SceneView.lastActiveSceneView;
                if (sv == null && SceneView.sceneViews.Count > 0) sv = (SceneView)SceneView.sceneViews[0];
                if (sv == null) sv = EditorWindow.GetWindow<SceneView>();
                string look = Str(r, "look_at");
                string angle = (Str(r, "angle") ?? "").ToLowerInvariant();
                Vector3 pivot = sv.pivot;
                float size = sv.size;
                if (!string.IsNullOrEmpty(look))
                {
                    var go = FindObject(look);
                    var b = BoundsOf(go);
                    pivot = b.center;
                    size = Mathf.Max(b.extents.magnitude * 1.3f, 0.5f);
                }
                Quaternion rot = sv.rotation;
                switch (angle)
                {
                    case "top": rot = Quaternion.Euler(90, 0, 0); break;
                    case "front": rot = Quaternion.Euler(0, 0, 0); break;
                    case "back": rot = Quaternion.Euler(0, 180, 0); break;
                    case "left": rot = Quaternion.Euler(0, 90, 0); break;
                    case "right": rot = Quaternion.Euler(0, -90, 0); break;
                    case "": case "current": break;
                    default: rot = Quaternion.Euler(30, 45, 0); break;   // "iso" and anything else
                }
                sv.LookAtDirect(pivot, rot, size);
                sv.Repaint();
                // The scene view moves its own camera only when it next draws, so render from a
                // temporary camera placed where the scene view is now looking.
                temp = new GameObject("LocalAgentShot") { hideFlags = HideFlags.HideAndDontSave };
                var cam = temp.AddComponent<Camera>();
                cam.CopyFrom(sv.camera);
                cam.cameraType = CameraType.Game;
                cam.enabled = false;
                if (!string.IsNullOrEmpty(look) || angle != "")
                {
                    float dist = sv.orthographic ? size * 2f : size / Mathf.Sin(sv.camera.fieldOfView * 0.5f * Mathf.Deg2Rad);
                    cam.transform.SetPositionAndRotation(pivot - rot * Vector3.forward * dist, rot);
                    cam.orthographic = sv.orthographic;
                    cam.orthographicSize = size;
                    cam.nearClipPlane = Mathf.Max(0.01f, dist * 0.001f);
                    cam.farClipPlane = Mathf.Max(1000f, dist * 100f);
                }
                cams.Add(cam);
                note = "Scene view" + (string.IsNullOrEmpty(look) ? "" : " looking at " + look) +
                       (angle == "" ? "" : " from the " + angle) + ".";
            }
            var rt = RenderTexture.GetTemporary(w, h, 24, RenderTextureFormat.ARGB32);
            var prevActive = RenderTexture.active;
            var tex = new Texture2D(w, h, TextureFormat.RGB24, false);
            try
            {
                foreach (var c in cams)
                {
                    var prev = c.targetTexture;
                    float aspect = c.aspect;
                    c.targetTexture = rt;
                    c.aspect = (float)w / h;
                    c.Render();
                    c.targetTexture = prev;
                    c.aspect = aspect;
                    if (prev == null) c.ResetAspect();
                }
                RenderTexture.active = rt;
                tex.ReadPixels(new Rect(0, 0, w, h), 0, 0);
                tex.Apply();
                return new D { {"png", Convert.ToBase64String(tex.EncodeToPNG())}, {"width", w}, {"height", h}, {"note", note} };
            }
            finally
            {
                RenderTexture.active = prevActive;
                RenderTexture.ReleaseTemporary(rt);
                Object.DestroyImmediate(tex);
                if (temp != null) Object.DestroyImmediate(temp);
            }
        }

        static Bounds BoundsOf(GameObject go)
        {
            var rs = go.GetComponentsInChildren<Renderer>();
            if (rs.Length == 0) return new Bounds(go.transform.position, Vector3.one);
            var b = rs[0].bounds;
            foreach (var x in rs) b.Encapsulate(x.bounds);
            return b;
        }

        // ------------------------------------------------------------------ small helpers
        static string Str(D d, string k)
        {
            object v;
            if (!d.TryGetValue(k, out v) || v == null) return null;
            return v as string ?? Convert.ToString(v, Inv);
        }

        static double Num(D d, string k, double def)
        {
            object v;
            if (!d.TryGetValue(k, out v) || v == null) return def;
            try { return ToD(v); } catch (Exception) { return def; }
        }

        static bool Bool(D d, string k, bool def)
        {
            object v;
            return d.TryGetValue(k, out v) && v != null ? ToBool(v) : def;
        }

        static L List(D d, string k)
        {
            object v;
            if (!d.TryGetValue(k, out v) || v == null) return new L();
            if (v is L) return (L)v;
            if (v is string) return new L(((string)v).Split(',').Select(s => (object)s.Trim()).Where(s => (string)s != ""));
            return new L { v };
        }

        static double ToD(object v)
        {
            if (v is double) return (double)v;
            if (v is long) return (long)v;
            if (v is int) return (int)v;
            if (v is bool) return (bool)v ? 1 : 0;
            if (v is string) return double.Parse(((string)v).Trim(), NumberStyles.Float, Inv);
            throw new BridgeError("expected a number, got " + Json.Write(v));
        }

        static bool ToBool(object v)
        {
            if (v is bool) return (bool)v;
            if (v is string)
            {
                string s = ((string)v).Trim().ToLowerInvariant();
                return s == "true" || s == "yes" || s == "on" || s == "1";
            }
            return ToD(v) != 0;
        }

        static float[] Floats(object v, int need)
        {
            float[] f;
            if (v is L) f = ((L)v).Select(x => (float)ToD(x)).ToArray();
            else if (v is D)
            {
                var d = (D)v;
                var keys = new[] { "x", "y", "z", "w" };
                f = keys.Where(d.ContainsKey).Select(k => (float)ToD(d[k])).ToArray();
            }
            else if (v is string)
                f = ((string)v).Trim('(', ')', '[', ']', ' ').Split(',').Select(s => float.Parse(s.Trim(), NumberStyles.Float, Inv)).ToArray();
            else if (v is double || v is long) f = Enumerable.Repeat((float)ToD(v), need).ToArray();
            else throw new BridgeError("expected " + need + " numbers like [1, 2, 3]");
            if (f.Length < need) throw new BridgeError("expected " + need + " numbers, got " + f.Length);
            return f;
        }

        static Vector3 ToVector3(object v) { var f = Floats(v, 3); return new Vector3(f[0], f[1], f[2]); }

        static Vector4 ToVector4(object v)
        {
            var f = Floats(v, 2);
            return new Vector4(f[0], f[1], f.Length > 2 ? f[2] : 0, f.Length > 3 ? f[3] : 0);
        }

        static Color ToColor(object v)
        {
            if (v is string)
            {
                Color c;
                string s = ((string)v).Trim();
                if (ColorUtility.TryParseHtmlString(s, out c)) return c;
                if (!s.StartsWith("#") && ColorUtility.TryParseHtmlString("#" + s, out c)) return c;
                if (!s.Contains(",")) throw new BridgeError("colors look like \"#ff8800\", \"red\" or [1, 0.5, 0, 1]");
            }
            var f = Floats(v, 3);
            bool bytes = f.Any(x => x > 1.0f) && f.All(x => x <= 255f && x == Mathf.Round(x));
            if (bytes) for (int i = 0; i < f.Length; i++) f[i] /= 255f;
            return new Color(f[0], f[1], f[2], f.Length > 3 ? f[3] : 1f);
        }

        static bool TryEnum<T>(string s, out T value) where T : struct
        {
            return Enum.TryParse(s, true, out value) && Enum.IsDefined(typeof(T), value);
        }
    }

    /// <summary>A small JSON reader and writer, so the bridge needs no packages.</summary>
    public static class Json
    {
        public static object Parse(string s)
        {
            int i = 0;
            object v = Value(s, ref i);
            Skip(s, ref i);
            if (i != s.Length) throw new FormatException("Extra text after JSON at " + i);
            return v;
        }

        static void Skip(string s, ref int i)
        {
            while (i < s.Length && char.IsWhiteSpace(s[i])) i++;
        }

        static object Value(string s, ref int i)
        {
            Skip(s, ref i);
            if (i >= s.Length) throw new FormatException("Unexpected end of JSON");
            char c = s[i];
            if (c == '{')
            {
                var d = new Dictionary<string, object>();
                i++;
                Skip(s, ref i);
                if (i < s.Length && s[i] == '}') { i++; return d; }
                while (true)
                {
                    Skip(s, ref i);
                    if (i >= s.Length || s[i] != '"') throw new FormatException("Expected a key at " + i);
                    string k = Str(s, ref i);
                    Skip(s, ref i);
                    if (i >= s.Length || s[i] != ':') throw new FormatException("Expected ':' at " + i);
                    i++;
                    d[k] = Value(s, ref i);
                    Skip(s, ref i);
                    if (i < s.Length && s[i] == ',') { i++; continue; }
                    if (i < s.Length && s[i] == '}') { i++; return d; }
                    throw new FormatException("Expected ',' or '}' at " + i);
                }
            }
            if (c == '[')
            {
                var l = new List<object>();
                i++;
                Skip(s, ref i);
                if (i < s.Length && s[i] == ']') { i++; return l; }
                while (true)
                {
                    l.Add(Value(s, ref i));
                    Skip(s, ref i);
                    if (i < s.Length && s[i] == ',') { i++; continue; }
                    if (i < s.Length && s[i] == ']') { i++; return l; }
                    throw new FormatException("Expected ',' or ']' at " + i);
                }
            }
            if (c == '"') return Str(s, ref i);
            if (Word(s, ref i, "true")) return true;
            if (Word(s, ref i, "false")) return false;
            if (Word(s, ref i, "null")) return null;
            int start = i;
            while (i < s.Length && "+-0123456789.eE".IndexOf(s[i]) >= 0) i++;
            string num = s.Substring(start, i - start);
            if (num == "") throw new FormatException("Unexpected '" + c + "' at " + start);
            long lv;
            if (num.IndexOfAny(new[] { '.', 'e', 'E' }) < 0 && long.TryParse(num, NumberStyles.Integer, CultureInfo.InvariantCulture, out lv))
                return lv;
            return double.Parse(num, NumberStyles.Float, CultureInfo.InvariantCulture);
        }

        static bool Word(string s, ref int i, string w)
        {
            if (string.CompareOrdinal(s, i, w, 0, w.Length) != 0) return false;
            i += w.Length;
            return true;
        }

        static string Str(string s, ref int i)
        {
            var sb = new StringBuilder();
            i++;
            while (i < s.Length)
            {
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                if (i >= s.Length) break;
                char e = s[i++];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 't': sb.Append('\t'); break;
                    case 'r': sb.Append('\r'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'u':
                        if (i + 4 > s.Length) throw new FormatException("Bad \\u escape");
                        sb.Append((char)Convert.ToInt32(s.Substring(i, 4), 16));
                        i += 4;
                        break;
                    default: sb.Append(e); break;
                }
            }
            throw new FormatException("Unterminated string");
        }

        public static string Write(object o)
        {
            var sb = new StringBuilder();
            Write(sb, o);
            return sb.ToString();
        }

        static void Write(StringBuilder sb, object o)
        {
            if (o == null) { sb.Append("null"); return; }
            if (o is string) { Quote(sb, (string)o); return; }
            if (o is bool) { sb.Append((bool)o ? "true" : "false"); return; }
            if (o is float)
            {
                float f = (float)o;
                if (float.IsNaN(f) || float.IsInfinity(f)) sb.Append("null");
                else sb.Append(f.ToString("R", CultureInfo.InvariantCulture));
                return;
            }
            if (o is double)
            {
                double d = (double)o;
                if (double.IsNaN(d) || double.IsInfinity(d)) sb.Append("null");
                else sb.Append(d.ToString("R", CultureInfo.InvariantCulture));
                return;
            }
            if (o is int || o is long || o is short || o is byte || o is uint || o is ulong)
            {
                sb.Append(Convert.ToString(o, CultureInfo.InvariantCulture));
                return;
            }
            var dict = o as IDictionary;
            if (dict != null)
            {
                sb.Append('{');
                bool first = true;
                foreach (DictionaryEntry kv in dict)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    Quote(sb, Convert.ToString(kv.Key, CultureInfo.InvariantCulture));
                    sb.Append(':');
                    Write(sb, kv.Value);
                }
                sb.Append('}');
                return;
            }
            var list = o as IEnumerable;
            if (list != null)
            {
                sb.Append('[');
                bool first = true;
                foreach (var x in list)
                {
                    if (!first) sb.Append(',');
                    first = false;
                    Write(sb, x);
                }
                sb.Append(']');
                return;
            }
            Quote(sb, Convert.ToString(o, CultureInfo.InvariantCulture));
        }

        static void Quote(StringBuilder sb, string s)
        {
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }
}
#endif
