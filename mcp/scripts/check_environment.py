"""Check imports only; this does not load models or run NMR predictions."""

import os
import sys
import warnings

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

import h5py
import keras
import numpy
import pandas
import scipy
import sklearn
import tensorflow
from rdkit import rdBase

from cascade.apply import predict_NMR_C, predict_NMR_H

print("Python: {}".format(sys.version))
for name, version in [
    ("RDKit", rdBase.rdkitVersion),
    ("TensorFlow", tensorflow.__version__),
    ("Keras", keras.__version__),
    ("NumPy", numpy.__version__),
    ("pandas", pandas.__version__),
    ("SciPy", scipy.__version__),
    ("scikit-learn", sklearn.__version__),
    ("h5py", h5py.__version__),
]:
    print("{}: {}".format(name, version))
print("IMPORT OK")
