# Installing Yosys (from source)

The synth/metric runners in this repo call `yosys`. The Ubuntu apt package lags badly,
so build from source. This is the flow we use.

## Conventions this repo assumes

- **Source tree lives at `/src/tools/yosys`.** The synth TCL templates hardcode the toy
  liberty from the yosys source tree, e.g. `synth/yosys_synth.tcl`:
  ```tcl
  set stdcell_lib "/src/tools/yosys/examples/cmos/cmos_cells.lib"
  ```
  so the source must stay on disk at that path (see [Keep the source](#keep-the-source)).
- **Binaries install to a versioned prefix** `/opt/yosys/<tag>`, with a `current` symlink
  you flip to switch versions. Only `/opt/yosys/current/bin` goes on `PATH`.

## Prerequisites

Yosys **v0.45+** builds with **CMake** (the old `make PREFIX=...` flow is gone). Minimums:
CMake >= 3.28, Bison >= 3.8, Python >= 3.11, and a C++20 compiler (GCC 15 or clang).

On Ubuntu:
```bash
sudo apt install -y build-essential cmake ninja-build bison flex \
     libreadline-dev gawk tcl-dev libffi-dev pkg-config zlib1g-dev git
```
`ninja-build` is optional but faster than make; `clang`/`lld`/`boost`/`graphviz` are not
needed for this repo's flow.

> Older Ubuntu LTS ships CMake < 3.28 via apt. If `cmake --version` is too old, use
> `sudo snap install cmake --classic` instead.

## First install

```bash
# 1. one-time dirs (source tree + install root, owned by you so no sudo later)
sudo mkdir -p /src/tools /opt/yosys && sudo chown "$USER" /src/tools /opt/yosys

# 2. clone WITH submodules (yosys bundles ABC and the slang SV frontend as submodules)
git clone --recursive https://github.com/YosysHQ/yosys.git /src/tools/yosys
cd /src/tools/yosys

# 3. pick the latest release tag and check it out
git fetch --tags
git tag | grep -E '^v[0-9]' | sort -V | tail -1     # e.g. v0.67
git checkout v0.67
git submodule update --init --recursive             # sync submodules TO this tag

# 4. configure, build, install
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/yosys/v0.67
cmake --build build -j"$(nproc)"
cmake --install build

# 5. make it the active version
ln -sfn v0.67 /opt/yosys/current
```

Add to `~/.bashrc` (once):
```bash
export PATH="/opt/yosys/current/bin:$PATH"
```
Verify: `yosys --version`.

## Updating to a new version

```bash
cd /src/tools/yosys
git fetch --tags
git checkout <new-tag>                 # e.g. v0.68
git submodule update --init --recursive
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/yosys/<new-tag>
cmake --build build -j"$(nproc)"       # incremental: ninja reuses unchanged objects
cmake --install build
ln -sfn <new-tag> /opt/yosys/current   # flip active version
```

Rollback is just flipping the symlink back: `ln -sfn <old-tag> /opt/yosys/current`.
Keep old `/opt/yosys/<tag>` dirs until you trust the new one, then `rm -rf` the stale ones.

## Keep the source

**Yes, keep `/src/tools/yosys`.** It is needed at runtime (the synth TCL reads
`examples/cmos/*` from it) and it is your build tree (incremental rebuilds are much faster
than re-cloning, and `git checkout <tag>` lets you switch/bisect versions in place).

## Gotchas we hit

- **Tag naming changed.** Releases up to `yosys-0.44` used the `yosys-*` prefix; `v0.45+`
  use `v*`. `sort -V | tail` over the raw tag list buries the `v*` tags above the
  `yosys-*` ones, so filter with `grep -E '^v[0-9]'` (as above) to find the real latest.
- **"HEAD detached at v0.67" is normal** after checking out a tag; it just means you're on
  a fixed release, not a branch. Fine for building. Don't commit there. To silence the
  hint: `git config --global advice.detachedHead false`.
- **Stale `libs/symfpu/` after checkout.** Switching from `main` to a release can leave an
  untracked `libs/symfpu/` dir (it is not a submodule on recent tags). Safe to
  `rm -rf libs/symfpu`.
- **Always re-run `git submodule update --init --recursive` after `git checkout <tag>`**,
  or you build the submodule commits from the previous checkout (shows as `M libs/slang`,
  `M frontends/slang/lib` in `git status`).
- **slang is C++20 and RAM-heavy.** The slang frontend translation units are the slow part
  of the build. If a compile gets OOM-killed, lower parallelism (`cmake --build build -j6`);
  ninja resumes where it stopped. If you don't need it, `-DENABLE_SLANG=OFF` at configure.

## Portability note

The `/src/tools/yosys/examples/...` liberty path baked into `synth/*.tcl` is
machine-specific. If you build the source somewhere else, either symlink it to
`/src/tools/yosys` or update the `stdcell_lib`/`stdcell_v` paths in the TCL templates.
