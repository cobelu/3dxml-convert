#!/usr/bin/env python3
"""Export tessellated parts from an XML based 3DXML assembly to STL."""
import argparse
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import trimesh

NS = "{http://www.3ds.com/xsd/3DXML}"


def local(tag):
    return tag.rsplit("}", 1)[-1]


def children_index(root):
    refs, insts, reps = {}, {}, {}
    for e in root.iter():
        typ = local(e.tag)
        if typ not in ("Reference3D", "Instance3D", "InstanceRep"):
            continue
        info = {"element": e, "name": e.get("name", ""), "parent": None, "target": None, "matrix": np.eye(4)}
        for c in e:
            t = local(c.tag)
            if t == "IsAggregatedBy": info["parent"] = (c.text or "").strip()
            elif t == "IsInstanceOf": info["target"] = (c.text or "").strip()
            elif t == "RelativeMatrix":
                a = np.fromstring(c.text or "", sep=" ")
                if a.size == 12:
                    # 3DXML stores the local X/Y/Z axes, then translation.
                    # Axis vectors are matrix columns in our column-vector convention.
                    info["matrix"][:3, :3] = a[:9].reshape(3, 3).T
                    info["matrix"][:3, 3] = a[9:12]
        {"Reference3D": refs, "Instance3D": insts, "InstanceRep": reps}[typ][e.get("id")] = info
    return refs, insts, reps


def parse_geometry(data):
    root = ET.fromstring(data)
    verts, faces = [], []
    # In 3DXML 1.2, Faces and VertexBuffer are siblings under each
    # PolygonalRep. Face.strips indices refer to that rep's Positions array.
    for rep in root.iter(NS + "Rep"):
        if not rep.get("{http://www.w3.org/2001/XMLSchema-instance}type", "").endswith("PolygonalRepType"):
            continue
        vertex_buffer = next((x for x in rep if local(x.tag) == "VertexBuffer"), None)
        if vertex_buffer is None:
            continue
        positions = next((x for x in vertex_buffer if local(x.tag) == "Positions"), None)
        if positions is None or not positions.text:
            continue
        points = np.asarray(
            [[float(v) for v in p.split()] for p in positions.text.strip().split(",") if p.strip()],
            dtype=float,
        )
        if not len(points):
            continue
        offset = len(verts)
        verts.extend(points)
        for face in rep.iter(NS + "Face"):
            strips = face.get("strips")
            if not strips:
                continue
            for strip in strips.split(","):
                ids = [int(x) + offset for x in strip.split()]
                if any(index < offset or index >= offset + len(points) for index in ids):
                    raise ValueError("Face strip index is outside its VertexBuffer Positions")
                for i in range(len(ids) - 2):
                    tri = (ids[i], ids[i+1], ids[i+2]) if i % 2 == 0 else (ids[i+1], ids[i], ids[i+2])
                    if len(set(tri)) == 3:
                        faces.append(tri)
    if not faces:
        raise ValueError("no triangulated Face strips found")
    return trimesh.Trimesh(vertices=np.asarray(verts), faces=np.asarray(faces), process=False)


def safe_name(s):
    s = re.sub(r"[^\w.-]+", "_", s.strip(), flags=re.UNICODE).strip("._")
    return (s or "part")[:100]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", "-o", type=Path, default=Path("stl"))
    ap.add_argument("--center", action="store_true", help="center each exported mesh at its own bounding-box center")
    ap.add_argument("--unique", action="store_true", help="export one local-coordinate STL per referenced part, deduplicating repeated instances")
    args = ap.parse_args()
    report = []
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(args.input)
    except Exception as exc:
        ap.error(f"cannot open 3DXML ZIP container: {exc}")
    with archive:
        names = set(archive.namelist())
        docname = "NameSetTree.3dxml" if "NameSetTree.3dxml" in names else None
        if not docname:
            ap.error("no NameSetTree.3dxml assembly document found")
        root = ET.fromstring(archive.read(docname))
        refs, insts, instreps = children_index(root)
        repfiles = {}
        for e in root.iter(NS + "ReferenceRep"):
            repfiles[e.get("id")] = e.get("associatedFile", "").replace("urn:3DXML:", "")
        reps_by_ref, children = {}, {}
        for iid, item in instreps.items():
            if item["target"]: reps_by_ref.setdefault(item["parent"], []).append(item["target"])
        for iid, item in insts.items():
            if item["parent"]: children.setdefault(item["parent"], []).append(iid)

        geom_cache = {}
        unique_written = set()
        count = 0
        def export(refid, label, matrix, seen):
            nonlocal count
            if refid in seen:
                report.append(f"Skipped recursive assembly reference {refid} ({label})")
                return
            new_seen = seen | {refid}
            geom_ids = reps_by_ref.get(refid, [])
            for rid in geom_ids:
                if args.unique and rid in unique_written:
                    continue
                fname = repfiles.get(rid, "")
                if not fname or fname not in names:
                    report.append(f"Missing geometry file for representation {rid} on {label}")
                    continue
                try:
                    if fname not in geom_cache: geom_cache[fname] = parse_geometry(archive.read(fname))
                    mesh = geom_cache[fname].copy()
                    if not args.unique: mesh.apply_transform(matrix)
                    if args.center: mesh.apply_translation(-mesh.bounding_box.centroid)
                    count += 1
                    base = safe_name(refs.get(refid, {}).get("name", "part") if args.unique else (label or refs.get(refid, {}).get("name", "part")))
                    suffix = "" if count == 1 else f"_{count:03d}"
                    path = args.output / f"{base}{suffix}.stl"
                    mesh.export(path)
                    unique_written.add(rid)
                except Exception as exc:
                    report.append(f"Could not decode {fname} ({label}): {exc}")
            for iid in children.get(refid, []):
                child = insts[iid]
                target = child["target"]
                if not target or target not in refs:
                    report.append(f"Instance {child['name'] or iid} has missing product reference {target}")
                    continue
                child_matrix = matrix @ child["matrix"]
                export(target, child["name"] or refs[target]["name"], child_matrix, new_seen)

        rootid = root.find(NS + "ProductStructure").get("root")
        if rootid not in refs:
            ap.error(f"assembly root reference {rootid} was not found")
        # A root Reference3D can itself have a representation; traversal includes it.
        export(rootid, refs[rootid]["name"], np.eye(4), set())
    print(f"Exported {count} STL file(s) to {args.output}")
    report_path = args.output / "conversion_report.txt"
    report_path.write_text("\n".join(report) if report else "No decoding issues reported.\n", encoding="utf-8")
    if report: print(f"{len(report)} issue(s) recorded in {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
