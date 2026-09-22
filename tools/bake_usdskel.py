"""Bake a UsdSkel-animated character into a plain, skeleton-free USD file.

ovrtx_server refuses to play back any scene containing a Skeleton/SkelRoot
prim: driving the animation clock (update_from_usd_time) on UsdSkel joint
animation reliably crashes the native ovrtx/ovstage renderer process in the
currently installed build (ovrtx 0.5.0 / ovstage 0.2.0), with no way to catch
or recover from that crash from Python (see app/render_session.py, anim_unsafe).

This script works around that by using USD's own UsdSkel.BakeSkinning to
convert the joint-driven deformation into ordinary per-frame mesh point time
samples, then strips every skeleton-related prim/schema so the result plays
back exactly like any other keyframed mesh (which ovrtx already handles fine).

Requires the `usd-core` package (pip install usd-core) for the `pxr` module;
ovrtx_server itself does not depend on it at runtime, only this offline tool.

Usage:
    python tools/bake_usdskel.py path/to/Scene.usd
    python tools/bake_usdskel.py path/to/Scene.usd -o path/to/Scene.baked.usd
"""

from __future__ import annotations

import argparse
import os

from pxr import Gf, Usd, UsdSkel


def bake_usdskel(src_path: str, dst_path: str) -> None:
    stage = Usd.Stage.Open(src_path, load=Usd.Stage.LoadAll)
    if stage is None:
        raise RuntimeError(f"Could not open stage: {src_path}")

    skel_roots = [prim for prim in stage.Traverse() if prim.IsA(UsdSkel.Root)]
    if not skel_roots:
        raise RuntimeError(f"No SkelRoot found in {src_path}; nothing to bake")

    for prim in skel_roots:
        # BakeSkinning also retypes the SkelRoot prim itself to Xform.
        if not UsdSkel.BakeSkinning(UsdSkel.Root(prim), Gf.Interval.GetFullInterval()):
            raise RuntimeError(f"BakeSkinning failed for {prim.GetPath()}")

    for prim in stage.Traverse():
        if prim.HasAPI(UsdSkel.BindingAPI):
            prim.RemoveAPI(UsdSkel.BindingAPI)
        if prim.GetTypeName() in ("Skeleton", "SkelAnimation"):
            # Skeleton/SkelAnimation prims are typically composed in through a
            # reference/payload (e.g. a shared rig.usd), so RemovePrim can't
            # delete them -- it only edits the local layer stack, which has no
            # opinion to remove. Deactivating suppresses a referenced prim
            # regardless of which layer defines it.
            prim.SetActive(False)

    flattened = stage.Flatten()
    flattened.Export(dst_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src", help="Path to the source .usd/.usda/.usdc file containing a SkelRoot")
    parser.add_argument("-o", "--output", help="Output path (default: <src stem>.baked.usd next to the source)")
    args = parser.parse_args()

    src = os.path.abspath(args.src)
    if args.output:
        dst = os.path.abspath(args.output)
    else:
        root, _ = os.path.splitext(src)
        dst = f"{root}.baked.usd"

    bake_usdskel(src, dst)
    print(f"Baked {src} -> {dst}")


if __name__ == "__main__":
    main()
