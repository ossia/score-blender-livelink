// Derives livelink-demo.score from score-docs' scene-graph-filter example: the
// same PBR render chain, with its Asset Loader, filter, camera and light
// replaced by a glTF Live Link, so that everything shown comes from Blender.
//
// Run from this directory, which is where livelink-demo.score is written,
// then give its render pipeline skinning, which the example lacks:
//
//   ossia-score --no-restore <score-docs>/assets/scores/examples/3d/scene-graph-filter.score \
//               --script make_demo_score.js
//   python3 add_skinning.py livelink-demo.score

var out = "livelink-demo.score";

function must(x, what) {
  if (!x) { console.log("DEMO-ERROR: no " + what); Qt.exit(1); }
  return x;
}

var pre = must(Score.find("Scene Preprocessor.8"), "Scene Preprocessor");
["Asset Loader.2", "Scene Graph Filter.3", "Camera.6", "Light.7"].forEach(function(n) {
  Score.remove(must(Score.find(n), n));
});

var root = Score.rootInterval();
var link = must(Score.createProcess(root, "b9446bac-dbeb-4672-a9b6-db6e6cdaef2e", ""), "glTF Live Link");
must(Score.createCable(Score.outlet(link, 0), Score.inlet(pre, "Scene In")), "cable");

// The example renders behind score's canvas (Window mode "Background"); a
// separate window is what one wants next to Blender.
Score.removeDevice("Window");
Score.createDevice("Window", "5a181207-7d40-4ad8-814e-879fcdf8cc31", { "Mode": 0, "InputWidth": 1280, "InputHeight": 720 });
must(Score.device("Window"), "Window device");
var rp = must(Score.find("Render Pipeline.9"), "Render Pipeline");
Score.setAddress(Score.outlet(rp, "Texture Out"), "Window:/");

Score.saveAs(out);
console.log("DEMO-OK " + out);
Qt.exit(0);
