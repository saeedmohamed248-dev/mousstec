"""openWakeWord's melspectrogram.onnx + embedding_model.onnx → one .npz of plain
arrays and a layer plan, for a numpy-only forward pass on the shop's PC."""
import sys
import numpy as np
import onnx
from onnx import numpy_helper

src, out = sys.argv[1], sys.argv[2]
mel = onnx.load(f"{src}/melspectrogram.onnx")
mi = {i.name: numpy_helper.to_array(i) for i in mel.graph.initializer}
arrays = {
    "stft_real": mi["0.stft.conv_real.weight"][:, 0, :].T.copy(),   # (512, 257)
    "stft_imag": mi["0.stft.conv_imag.weight"][:, 0, :].T.copy(),
    "mel_w": mi["1.melW"],                                           # (257, 32)
}
emb = onnx.load(f"{src}/embedding_model.onnx")
ei = {i.name: numpy_helper.to_array(i) for i in emb.graph.initializer}
plan = []
k = 0
for n in emb.graph.node:
    attrs = {a.name: onnx.helper.get_attribute_value(a) for a in n.attribute}
    if n.op_type == "Conv":
        w = ei[n.input[1]]
        b = ei[n.input[2]] if len(n.input) > 2 else np.zeros(w.shape[0], np.float32)
        pads = list(attrs.get("pads", [0, 0, 0, 0]))
        arrays[f"w{k}"], arrays[f"b{k}"] = w, b
        plan.append(("conv", k, pads[0], pads[1], pads[2], pads[3]))
        k += 1
    elif n.op_type == "LeakyRelu":
        assert abs(attrs["alpha"] - 0.2) < 1e-6
        plan.append(("leaky", 0, 0, 0, 0, 0))
    elif n.op_type == "Max":
        c = [ei[i] for i in n.input if i in ei][0]
        assert abs(float(c.ravel()[0]) + 0.4) < 1e-6
        plan.append(("floor", 0, 0, 0, 0, 0))
    elif n.op_type == "MaxPool":
        kh, kw = attrs["kernel_shape"]
        sh, sw = attrs["strides"]
        assert (kh, kw) == (sh, sw)
        plan.append(("pool", kh, kw, 0, 0, 0))
    elif n.op_type == "Reshape":
        pass
    else:
        raise SystemExit(f"unexpected op {n.op_type}")
codes = {"conv": 0, "leaky": 1, "floor": 2, "pool": 3}
arrays["plan"] = np.array([(codes[p[0]],) + p[1:] for p in plan], dtype=np.int32)
np.savez(out, **arrays)
print(len(plan), "steps,", k, "convs →", out)
