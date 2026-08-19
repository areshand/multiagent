use crate::state::atomic_write;
use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

const USAGE:&str="Usage:\n  multiagent prompt-bundle --orchestrator PATH --lifecycle PATH --output PATH\n\nBuilds the canonical initial orchestrator prompt from the role prompt and the\nmandatory implementation lifecycle playbook.";

pub fn run(args: &[String]) -> Result<(), String> {
    if args
        .iter()
        .any(|arg| matches!(arg.as_str(), "-h" | "--help"))
    {
        println!("{USAGE}");
        return Ok(());
    }
    let options = parse_options(args)?;
    let orchestrator = options
        .get("--orchestrator")
        .map(String::as_str)
        .unwrap_or("");
    let lifecycle = options.get("--lifecycle").map(String::as_str).unwrap_or("");
    let output = options.get("--output").map(String::as_str).unwrap_or("");
    if !Path::new(orchestrator).is_file() {
        return Err(format!("orchestrator prompt not found: {orchestrator}"));
    }
    if !Path::new(lifecycle).is_file() {
        return Err(format!("lifecycle prompt not found: {lifecycle}"));
    }
    if output.is_empty() {
        return Err("--output is required".into());
    }
    let role = fs::read_to_string(orchestrator).map_err(io_error("read orchestrator prompt"))?;
    let lifecycle_text =
        fs::read_to_string(lifecycle).map_err(io_error("read lifecycle prompt"))?;
    let text=format!("----- BEGIN ORCHESTRATOR ROLE -----\n\n{role}\n----- END ORCHESTRATOR ROLE -----\n\n----- BEGIN MANDATORY IMPLEMENTATION LIFECYCLE -----\n\n{lifecycle_text}\n----- END MANDATORY IMPLEMENTATION LIFECYCLE -----\n");
    atomic_write(Path::new(output), &text)?;
    println!("prompt bundle built\t{output}");
    Ok(())
}

fn parse_options(args: &[String]) -> Result<BTreeMap<String, String>, String> {
    let mut values = BTreeMap::new();
    let mut index = 0;
    while index < args.len() {
        let key = &args[index];
        if !matches!(key.as_str(), "--orchestrator" | "--lifecycle" | "--output") {
            return Err(format!("unknown argument: {key}"));
        }
        let value = args
            .get(index + 1)
            .ok_or_else(|| format!("{key} requires a value"))?;
        values.insert(key.clone(), value.clone());
        index += 2;
    }
    Ok(values)
}
fn io_error(action: &'static str) -> impl Fn(std::io::Error) -> String {
    move |error| format!("{action}: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unknown_options_are_rejected() {
        assert!(parse_options(&["--bad".into(), "value".into()]).is_err());
    }
}
