// Load the data (*.jsonl files) with mongosh only.
// Expected layout:
//   init_db.js
//   load_seed.js
//   schemas/   <- variables.jsonl, investigations.jsonl, experiments.jsonl, ...
// Run (from any folder):
//   mongosh "mongodb://localhost:27017/psi_science" init_db.js
//   mongosh "mongodb://localhost:27017/psi_science" load_seed.js
// To use another folder: SEED_DIR=/path/to/folder mongosh ... load_seed.js
const fs = require('fs');
const path = require('path');

// Folder of this script (falls back to the current folder if mongosh doesn't provide it)
const here = (typeof __dirname !== 'undefined' && __dirname) ? __dirname : process.cwd();
const dir = process.env.SEED_DIR || path.join(here, 'schemas');

if (!fs.existsSync(dir)) {
	throw new Error(`Folder not found: ${dir}. Put the .jsonl files in a 'schemas' folder next to load_seed.js.`);
}
if (!fs.readdirSync(dir).some(f => f.endsWith('.jsonl'))) {
	throw new Error(`No .jsonl data files in ${dir}. Copy the files from seed/ (e.g. experiments.jsonl) into it.`);
}

const order = ['variables', 'investigations', 'measurements_metadata', 'documents',
	'experiments', 'scientific_findings', 'model_registry'];

for (const name of order) {
	const file = path.join(dir, `${name}.jsonl`);
	if (!fs.existsSync(file)) {
		print(`skip ${name}: ${file} not found`);
		continue;
	}
	const docs = fs.readFileSync(file, 'utf8').split('\n')
		.filter(line => line.trim())
		.map(line => EJSON.parse(line));   // restores dates ({"$date": ...})
	const ops = docs.map(d => ({ replaceOne: { filter: { _id: d._id }, replacement: d, upsert: true } }));
	for (let i = 0; i < ops.length; i += 500) {
		db.getCollection(name).bulkWrite(ops.slice(i, i + 500), { ordered: false });
	}
	print(`${name}: ${docs.length} documents`);
}
