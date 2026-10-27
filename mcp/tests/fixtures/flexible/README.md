# Flexible conformer validation fixture

Generated locally from the unchanged CASCADE CLI for `CCCCO`, nucleus `13C`, using
the validated Python 3.5 / RDKit 2018.03.3 / Keras 2.2.4 / TensorFlow 1.12.0 image.
This particular run retained seven conformers and four carbon predictions.

The test reverses SDF records and still requires correspondence by `(mol_id, cf_id)`;
it then removes a conformer and requires validation failure. Do not assume future
conformer searches reproduce these counts, IDs, geometries, or rounded shift values.
The fixture is for mapping validation, not scientific accuracy benchmarking.

Upstream CASCADE is MIT licensed. Publication: Guan et al., *Chem. Sci.* **2021**,
DOI: 10.1039/D1SC03343C.
