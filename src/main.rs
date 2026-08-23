mod agent;
mod authority;
mod config;
mod dag;
mod decision;
mod policy;
mod prod_ops;
mod prompt_bundle;
mod role_sandbox;
mod runtime;
mod session_control;
mod snapshot;
mod state;
mod subagent;
mod supervisor;
mod workflow;

use std::env;
use std::process::ExitCode;

const USAGE: &str = r#"Usage:
  multiagent dag COMMAND [ARGS...]
  multiagent agent COMMAND [ARGS...]
  multiagent decision COMMAND [ARGS...]
  multiagent policy COMMAND [ARGS...]
  multiagent prompt-bundle [ARGS...]
  multiagent subagent COMMAND [ARGS...]
  multiagent workflow COMMAND [ARGS...]
  multiagent session-control SESSION status|capture|submit|stop [ARGS...]
  multiagent launch|orchestrator|status|watch [ARGS...]
  multiagent snapshot --root DIR [--base REV] [--format json|shell]

The Rust binary owns both durable control-plane state and tmux subprocess
orchestration. launch.sh is the source-checkout bootstrap."#;

fn main() -> ExitCode {
    let mut args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() || matches!(args[0].as_str(), "-h" | "--help" | "help") {
        println!("{USAGE}");
        return ExitCode::SUCCESS;
    }

    let command = args.remove(0);
    if let Err(message) = role_sandbox::gate_setuid_invocation(&command) {
        eprintln!("multiagent: {message}");
        return ExitCode::from(1);
    }
    if let Some(result) = supervisor::proxy_if_required(&command, &args) {
        return match result {
            Ok(code) => code,
            Err(message) => {
                eprintln!("supervisor: {message}");
                ExitCode::from(1)
            }
        };
    }
    let result: Result<ExitCode, (&str, String)> = match command.as_str() {
        "container-bootstrap" => {
            runtime::container_bootstrap().map_err(|message| ("container-bootstrap", message))
        }
        "agent" => agent::run(&args).map_err(|message| ("agent", message)),
        "launch" => runtime::launch(&args).map_err(|message| ("launch", message)),
        "orchestrator" => runtime::orchestrator(&args).map_err(|message| ("orchestrator", message)),
        "status" => runtime::status(&args).map_err(|message| ("status", message)),
        "watch" => runtime::watch(&args).map_err(|message| ("watch", message)),
        "dag" => dag::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("dag", message)),
        "decision" => decision::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("decision", message)),
        "policy" => policy::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("write-policy", message)),
        "ops" => prod_ops::run(&args).map_err(|message| ("ops", message)),
        "prompt-bundle" => prompt_bundle::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("prompt-bundle", message)),
        "role-exec" => role_sandbox::run(&args).map_err(|message| ("role-exec", message)),
        "role-agent-exec" => {
            runtime::role_agent_exec(&args).map_err(|message| ("role-agent-exec", message))
        }
        "session-control" => session_control::run(&args)
            .map_err(|message| ("session-control", message)),
        "authority-supervisor-exec" => supervisor::authority_supervisor_exec(&args)
            .map_err(|message| ("authority-supervisor-exec", message)),
        "snapshot" => snapshot::run(&args)
            .map(|_| ExitCode::SUCCESS)
            .map_err(|message| ("snapshot", message)),
        "subagent" => subagent::run(&args).map_err(|message| ("subagent", message)),
        "supervisor" => supervisor::run(&args).map_err(|message| ("supervisor", message)),
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
