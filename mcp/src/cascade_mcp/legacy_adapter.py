"""Python 3.5 adapter: validate remotely supplied input, reuse the unchanged CLI."""
import argparse
import csv
import json
import math
import os
import runpy
import sys


def validate_results(directory, nucleus):
    from rdkit import Chem
    from xml.etree import ElementTree
    folder = os.path.join(directory, nucleus)
    molecules = [m for m in Chem.SDMolSupplier(os.path.join(folder, "conformers.sdf"), removeHs=False)]
    if not molecules or any(m is None for m in molecules):
        raise ValueError("Invalid conformer SDF")
    number = 1 if nucleus == "1H" else 6
    expected = None
    atom_numbers = None
    conformers = set()
    for molecule in molecules:
        selected = set(a.GetIdx() + 1 for a in molecule.GetAtoms() if a.GetAtomicNum() == number)
        if expected is None:
            expected = selected
            atom_numbers = [a.GetAtomicNum() for a in molecule.GetAtoms()]
        if not selected or selected != expected:
            raise ValueError("SDF atom mapping mismatch")
        if [a.GetAtomicNum() for a in molecule.GetAtoms()] != atom_numbers:
            raise ValueError("SDF atom ordering differs across conformers")
        geometry = molecule.GetConformer()
        for atom in molecule.GetAtoms():
            point = geometry.GetAtomPosition(atom.GetIdx())
            if not all(math.isfinite(v) for v in (point.x, point.y, point.z)):
                raise ValueError("Invalid 3D coordinates")
        identity = tuple(int(v) for v in molecule.GetProp("_Name").split("_"))
        if len(identity) != 2 or identity in conformers:
            raise ValueError("Duplicate/invalid conformer identity")
        conformers.add(identity)
    with open(os.path.join(folder, "weighted_shift.csv")) as stream:
        weighted = list(csv.DictReader(stream))
    ids = [int(r["atom_index"]) for r in weighted]
    if set(ids) != expected or len(ids) != len(expected) or any(not math.isfinite(float(r["Shift"])) for r in weighted):
        raise ValueError("Invalid weighted shifts")
    if {int(r["mol_id"]) for r in weighted} != {identity[0] for identity in conformers}:
        raise ValueError("Molecule IDs disagree")
    rows = {}
    with open(os.path.join(folder, "conformers_shift.csv")) as stream:
        for row in csv.DictReader(stream):
            key = (int(row["mol_id"]), int(row["cf_id"]))
            atom = int(row["atom_index"])
            if atom in rows.setdefault(key, set()):
                raise ValueError("Duplicate conformer atom")
            rows[key].add(atom)
            if any(not math.isfinite(float(row[k])) for k in ("relative_E", "predicted", "b_weight")):
                raise ValueError("Non-finite conformer values")
    if set(rows) != conformers or any(ids != expected for ids in rows.values()):
        raise ValueError("Conformer IDs or atom sets disagree")
    if not ElementTree.parse(os.path.join(folder, "prediction_2d.svg")).getroot().tag.endswith("svg"):
        raise ValueError("Invalid SVG")
    return {"atom_count": len(expected), "conformer_count": len(conformers), "unit": "ppm",
            "atom_shifts_preview": [{"atom_index": int(r["atom_index"]), "shift_ppm": float(r["Shift"])}
                                    for r in weighted[:10]], "preview_truncated": len(weighted) > 10}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-atoms", required=True, type=int)
    args = parser.parse_args()
    from rdkit import Chem
    with open(args.request) as stream:
        request = json.load(stream)
    molecule = Chem.MolFromSmiles(request["smiles"])
    nucleus = request["nuclei"][0]
    if (molecule is None or not molecule.GetNumAtoms() or len(Chem.GetMolFrags(molecule)) != 1 or
            any(a.GetAtomicNum() not in (1, 6, 7, 8, 9, 15, 16, 17) or a.GetFormalCharge()
                for a in molecule.GetAtoms())):
        return 20
    molecule = Chem.AddHs(molecule)
    if molecule.GetNumAtoms() > args.max_atoms or not any(a.GetAtomicNum() == (1 if nucleus == "1H" else 6)
                                                       for a in molecule.GetAtoms()):
        return 20
    sys.argv = ["predict_2d.py", "--smiles", request["smiles"], "--nuclei", nucleus, "--output", args.output]
    runpy.run_path("/opt/cascade-test/predict_2d.py", run_name="__main__")
    try:
        result = validate_results(args.output, nucleus)
    except Exception:
        import traceback
        traceback.print_exc()
        return 21
    with open(os.path.join(args.output, "validation.json"), "w") as stream:
        json.dump({nucleus: result}, stream)
    return 0


if __name__ == "__main__":
    sys.exit(main())
