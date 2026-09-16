import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

// Codex Grid: bottom-anchored cover (03), open single-column content (25).
// All content is native editable text; no raster decoration or external assets.
const sha = 'c7be3a5abb299ba409cad31d469883cd52284969';
const out = process.env.MAC_DECK_OUT || path.join(os.tmpdir(), 'mac-v1.5.0-slides');
await fs.mkdir(out, {recursive: true});
const p = Presentation.create({slideSize: {width:1280,height:720}});
const color='#142D25', muted='#416052';
function txt(s, value, x,y,w,h,size=32,bold=false,c=color) {
 const t=s.shapes.add({geometry:'textbox',position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 t.text=value; t.text.style={fontFamily:'Arial',fontSize:size,bold,color:c}; return t;
}
function page(title, rows, n) {
 const s=p.slides.add();s.background.fill='#FAFCF8';
 txt(s,title,56,40,1168,120,52,true);
 rows.forEach(([heading,body],i)=>{const y=200+i*126;txt(s,heading,56,y,1168,40,29,true);txt(s,body,56,y+45,1140,68,27,false,muted);});
 txt(s,`MAC v1.5.0 · ${sha.slice(0,12)} · 14 September 2026`,56,663,1050,30,20,false,muted);
 txt(s,String(n).padStart(2,'0'),1150,661,72,32,22,false,muted);
}
const cover=p.slides.add(); cover.background.fill='#DCEAD7';
txt(cover,'MAC  /  v1.5.0',56,48,1100,58,34,true);
txt(cover,'Capabilities and release evidence',56,122,1100,56,30);
txt(cover,'Verify before\npublication',56,355,1168,220,88,true);
txt(cover,`14 September 2026 · source ${sha.slice(0,12)}`,56,651,1168,38,23,false,muted);
page('A durable request-to-result path',[
 ['Work has an owner and a state','Projects, tasks, dependencies and leases live in the hub ledger.'],
 ['Execution leaves inspectable evidence','Tests, repository identity and artifacts accompany the result.'],
 ['Completion has distinct boundaries','Independent verification, publication and acceptance remain visible.'],
],2);
page('Hermes conversation; MAC execution',[
 ['Preserve the selected chat profile','Deployment uses the upstream Hermes service and its configured home.'],
 ['Health follows the configured runtime','Retired OpenClaw probes no longer degrade Hermes workers.'],
 ['Recovery stays independently observable','The crash observer uses its own managed Python 3.14.7 interpreter.'],
],3);
page('Isolate concurrent verification',[
 ['One PostgreSQL database per pytest worker','Parallel workers avoid sharing mutable test-ledger state.'],
 ['Owned resources have a bounded lifetime','Test setup and teardown identify and clean their own databases.'],
 ['Production remains a separate authority','These changes improve test contention; they do not replace hub capacity planning.'],
],4);
page('All testing happens up front',[
 ['Every candidate receives the full suite','Pull requests and main pushes run contract tests and historical fault replay.'],
 ['Container and documentation boundaries run too','ARM64 documentation smoke builds the candidate locally before using it.'],
 ['Publication waits for validation','Image qualification and documentation publication depend on their checks.'],
],5);
page('Tests must detect the old failure',[
 ['A historical regression is replayed twice','The current tree progresses to independent hub verification.'],
 ['The pre-fix tree must fail for the right reason','One physical worker remains waiting for a reviewer on the historical tree.'],
 ['An unrelated exception is not a successful oracle','The probe preserves the reviewer-starvation signal rather than masking setup errors.'],
],6);
page('Documentation follows the source',[
 ['PostgreSQL is the live authority','Startup verifies schema; migrations are an explicit deployment operation.'],
 ['The hub console is read-only','The optional Workbench prototype and desktop bridge have separate boundaries.'],
 ['Historical reports stay dated','Incident snapshots, retired project examples and proposed ADRs are not live status.'],
],7);
page('Release scope and remaining limits',[
 ['A release artifact is not a fleet cutover','Deployment still requires attestation and end-to-end canary evidence.'],
 ['Unfinished features remain outside this release','SoulGraph, Mission Control and automatic hold release remain deferred PRs.'],
 ['Compatibility claims retain their evidence limits','Focused Hermes compatibility passes do not imply its entire upstream suite passed.'],
],8);
for (const [i,s] of p.slides.items.entries()) {
 const stem=`slide-${String(i+1).padStart(2,'0')}`;
 const png=await p.export({slide:s,format:'png',scale:1});
 await fs.writeFile(path.join(out,stem+'.png'),new Uint8Array(await png.arrayBuffer()));
 const layout=await s.export({format:'layout'});await fs.writeFile(path.join(out,stem+'.layout.json'),await layout.text());
}
const pptx=await PresentationFile.exportPptx(p);await pptx.save(path.join(out,'mac-capabilities.pptx'));
console.log(`Exported ${p.slides.items.length} slides to ${out}`);
