# 3DXML to STL

`3dxml_to_stl.py` exports tessellated geometry from XML-based 3DXML assemblies. It reads the assembly tree and component placements from `NameSetTree.3dxml`, loads the referenced `TessPart_*.3DRep` files, and writes STL meshes.

## Set up the environment

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Activate the environment again in a new terminal with `source .venv/bin/activate`.

## Use

```sh
python 3dxml_to_stl.py assembly.3dxml --output ./stl
```

By default, each component occurrence is exported as its own STL in assembly coordinates, so repeated components appear at their placed positions. `--center` shifts each exported mesh to its own bounding-box center. `--unique` writes only one local-coordinate STL for each distinct representation, which is useful when you need a part library rather than an assembled layout. Any missing or undecodable representations are recorded in `conversion_report.txt`.

