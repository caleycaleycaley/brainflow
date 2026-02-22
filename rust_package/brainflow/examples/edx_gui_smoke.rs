#[path = "plot_rt_common/mod.rs"]
mod plot_rt_common;

fn main() {
    // Compatibility wrapper: keep legacy example name, run the minimal realtime plot app.
    plot_rt_common::run_minimal_from_args();
}
