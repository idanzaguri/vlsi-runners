# UVM 1.2 DPI code for Verilator

UVM 1.2 has an HDL backend (`uvm_hdl_force`, `uvm_hdl_deposit`, `uvm_hdl_read`) for
Questa, VCS and Xcelium only. For any other simulator, its `dpi/uvm_hdl.c` stops at
`#error "hdl vendor backend is missing"`. `test_runner --simulator verilator` copies
`$UVM_HOME/src/dpi/` into the build directory, puts the two files below over the copy,
and compiles its `uvm_dpi.cc`.

| file | where it comes from |
|---|---|
| `uvm_hdl.c` | UVM 1.2 `src/dpi/uvm_hdl.c`, with the `#error` line replaced by `#include "uvm_hdl_verilator.c"` |
| `uvm_hdl_verilator.c` | `src/dpi/uvm_hdl_verilator.c` of the verilator/uvm repository (branch uvm-2020-3.1-vlt), unchanged |

Both files are under the Apache License 2.0 (see their headers). The ovip-dev session
validated them on 2026-10-25 with the OVIP AXI and APB testbenches.

The backend reaches signals through VPI. A signal that a test forces must be marked in a
Verilator configuration file (`.vlt`, the config.yaml key `verilator_config`):

```
`verilator_config
forceable      -module "tb" -var "rst_n"
public_flat_rw -module "tb" -var "rst_n"
```
