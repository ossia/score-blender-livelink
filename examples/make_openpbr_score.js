// Derives livelink-openpbr.score from score-corpus' sponza-openpbr example: its
// OpenPBR render chain (score-csf-testers' classic_pbr_openpbr with the
// OpenPBR LUTs of score-addon-academy, tone mapping), with its asset, camera
// and light replaced by a glTF Live Link. Needs score-addon-academy.
//
// Run from this directory, which is where livelink-openpbr.score is written,
// then give it score-csf-testers' current OpenPBR shader, with skinning:
//
//   ossia-score --no-restore <score-corpus>/2026/lgm/sponza-openpbr.score \
//               --script make_openpbr_score.js
//   python3 openpbr_shader.py livelink-openpbr.score <score-csf-testers>
//   python3 add_skinning.py livelink-openpbr.score

var out = "livelink-openpbr.score";

function must(x, what) {
  if (!x) { console.log("DEMO-ERROR: no " + what); Qt.exit(1); }
  return x;
}

var pre = must(Score.find("Scene Preprocessor"), "Scene Preprocessor");
["Sponza", "Camera", "Light"].forEach(function(n) {
  Score.remove(must(Score.find(n), n));
});

var root = Score.rootInterval();
var link = must(Score.createProcess(root, "b9446bac-dbeb-4672-a9b6-db6e6cdaef2e", ""), "glTF Live Link");
must(Score.createCable(Score.outlet(link, 0), Score.inlet(pre, "Scene In")), "cable");

Score.saveAs(out);
console.log("DEMO-OK " + out);
Qt.exit(0);
