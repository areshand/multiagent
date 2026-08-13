mod adapter;
mod config;
mod dag;
mod decision;
mod policy;
mod prompt_bundle;
mod snapshot;
mod subagent;
mod workflow;

use std::env;
use std::process::ExitCode;

const USAGE: &str = r#"Usage:
  multiagent dag COMMAND [ARGS...]
  multiagent decision COMMAND [ARGS...]
  multiagent policy COMMAND [ARGS...]
  multiagent prompt-bundle [ARGS...]
  multiagent subagent COMMAND [ARGS...]
  multiagent workflow COMMAND [ARGS...]
  multiagent launch|orchestrator|status|watch [ARGS...]
  multiagent snapshot --root DIR [--base REV] [--format json|shell]

The Rust core owns production control-plane state. Existing bin/*.sh entrypoints
are compatibility wrappers; tmux-oriented commands remain external adapters."#;

fn main() -> ExitCode {
    let mut args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || matches!(args[0].as_str(), "-h" | "--help" | "help") {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }

    let command = args.remove(0);
    let result: Result<ExitCode, (&str, String)> = match command.as_str() {
        "launch" => adapter::run("launch.sh", &args, &[]).map_err(|message| ("launch", message)),
        "orchestrator" => adapter::run("bin/orchestrator.sh", &args, &[])
            .map_err(|message| ("orchestrator", message)),
        "status" => {
            adapter::run("bin/status.sh", &args, &[]).map_err(|message| ("status", message))
        }
        "watch" => adapter::run("bin/watch.sh", &args, &[]).map_err(|message| ("watch", message)),
        "dag" => dag::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("dag", message)),
        "decision" => decision::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("decision", message)),
        "policy" => policy::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("write-policy", message)),
        "prompt-bundle" => prompt_bundle::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("prompt-bundle", message)),
        "snapshot" => snapshot::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("snapshot", message)),
        "subagent" => subagent::run(&args).map_err(|message| ("subagent", message)),
        "workflow" => workflow::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("workflow", message)),
        _ => {
            eprintln!("multiagent: unknown command: {command}");
            return ExitCode::from(1);
        }
    };

    match result {
        Ok(code) => code,
        Err((prefix, message)) => {
            if !message.is_empty() {
                eprintln!("{prefix}: {message}");
            }
            ExitCode::from(1)
        }
    }
}
