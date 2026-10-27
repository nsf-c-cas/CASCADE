"""Run upstream CASCADE inference and draw atom-level weighted shifts."""

import argparse
import os
import warnings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smiles", required=True)
    parser.add_argument("--nuclei", nargs="+", choices=["1H", "13C"], required=True)
    parser.add_argument("--output", required=True, help="Output directory, e.g. /output/example")
    args = parser.parse_args()

    # Hide known legacy dependency notices, while keeping other warnings/errors.
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "1")
    warnings.filterwarnings(
        "ignore", category=FutureWarning,
        message=r"Passing \(type, 1\) or '1type' as a synonym of type is deprecated;",
        module=r"tensorflow\.python\.framework\.dtypes",
    )
    warnings.filterwarnings(
        "ignore", category=FutureWarning,
        message=r"The sklearn\.preprocessing\.data module is\s+deprecated",
        module=r"sklearn\.utils\.deprecation",
    )

    import numpy as np
    from keras import backend as K
    from keras.models import load_model
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from rdkit.Chem.Draw import rdMolDraw2D
    from nfp.models import GraphModel
    from nfp.layers import (
        ReduceAtomToPro, Squeeze, GatherAtomToBond, ReduceBondToAtom
    )
    from cascade.apply import predict_NMR_H, predict_NMR_C

    mol = Chem.MolFromSmiles(args.smiles)
    if mol is None:
        parser.error("Invalid SMILES")
    if any(atom.GetAtomicNum() not in (1, 6, 7, 8, 9, 15, 16, 17)
           or atom.GetFormalCharge() != 0 for atom in mol.GetAtoms()):
        parser.error("CASCADE requires neutral atoms of C, H, N, O, F, P, S, or Cl")

    custom_objects = {
        "GraphModel": GraphModel,
        "ReduceAtomToPro": ReduceAtomToPro,
        "Squeeze": Squeeze,
        "GatherAtomToBond": GatherAtomToBond,
        "ReduceBondToAtom": ReduceBondToAtom,
    }
    models = {
        "1H": ("best_model_H_DFTNN.hdf5", predict_NMR_H, 1),
        "13C": ("best_model.hdf5", predict_NMR_C, 6),
    }
    for nucleus in dict.fromkeys(args.nuclei):
        filename, predict, atomic_number = models[nucleus]
        model = load_model(
            os.path.join("cascade", "trained_model", filename),
            custom_objects=custom_objects, compile=False,
        )
        try:
            mols, weighted, individual = predict(args.smiles, model)
        finally:
            K.clear_session()
        if not mols or weighted.empty or not np.isfinite(weighted["Shift"]).all():
            raise RuntimeError("No finite predictions returned for " + nucleus)

        shifts = dict(zip(weighted["atom_index"].astype(int), weighted["Shift"]))
        drawing = Chem.Mol(mols[0])
        expected = {atom.GetIdx() + 1 for atom in drawing.GetAtoms()
                    if atom.GetAtomicNum() == atomic_number}
        if set(shifts) != expected:
            raise RuntimeError("Prediction atom indices do not match the structure")
        # Preserve the upstream one-based atom IDs in drawing labels.
        for atom in drawing.GetAtoms():
            atom.SetIntProp("source_index", atom.GetIdx() + 1)
        AllChem.Compute2DCoords(drawing)
        drawing = rdMolDraw2D.PrepareMolForDrawing(drawing)

        output = os.path.abspath(os.path.join(args.output, nucleus))
        os.makedirs(output, exist_ok=True)
        weighted.to_csv(os.path.join(output, "weighted_shift.csv"), index=False)
        individual.to_csv(os.path.join(output, "conformers_shift.csv"), index=False)
        writer = Chem.SDWriter(os.path.join(output, "conformers.sdf"))
        for conformer in mols:
            writer.write(conformer)
        writer.close()

        for extension, renderer in [
            ("svg", rdMolDraw2D.MolDraw2DSVG),
            ("png", rdMolDraw2D.MolDraw2DCairo),
        ]:
            drawer = renderer(1200, 850)
            options = drawer.drawOptions()
            options.padding = 0.12
            for atom in drawing.GetAtoms():
                atom_id = atom.GetIntProp("source_index")
                if atom_id in shifts:
                    options.atomLabels[atom.GetIdx()] = "{}{}: {:.2f}".format(
                        atom.GetSymbol(), atom_id, shifts[atom_id]
                    )
            drawer.DrawMolecule(
                drawing, legend=nucleus + " weighted chemical shifts (ppm)"
            )
            drawer.FinishDrawing()
            with open(os.path.join(output, "prediction_2d." + extension),
                      "wb" if extension == "png" else "w") as handle:
                handle.write(drawer.GetDrawingText())
        print("{} OK: {} conformers, {} atom shifts; {}".format(
            nucleus, len(mols), len(weighted), output
        ))


if __name__ == "__main__":
    main()
