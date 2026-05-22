# Claude CLI Permission Configuration Investigation

## Permission-Related CLI Options Found

### 1. Permission Modes
- `--permission-mode <mode>` with choices:
  - "acceptEdits" 
  - "auto"
  - "bypassPermissions"
  - "default"
  - "dontAsk"
  - "plan"

### 2. Permission Bypass Options
- `--allow-dangerously-skip-permissions` - Enable bypassing as an option (not enabled by default)
- `--dangerously-skip-permissions` - Bypass all permission checks

### 3. Tool Access Control
- `--allowedTools, --allowed-tools <tools...>` - Allow specific tools (e.g. "Bash(git *) Edit")
- `--disallowedTools, --disallowed-tools <tools...>` - Deny specific tools
- `--tools <tools...>` - Specify available tools from built-in set

### 4. Other Security/Access Options  
- `--bare` - Minimal mode that skips many features including permission checks
- `--add-dir <directories...>` - Additional directories to allow tool access to

## Configuration Files Found

### Global Settings
- `~/.claude/settings.json` - Main global settings
- `~/.claude/settings.local.json` - Local permission overrides

### Project Settings
- `.claude/settings.json` - Project-specific settings (none found in this project)

## Permission Configuration Structure

### Global Settings (`~/.claude/settings.json`)
```json
{
  "model": "claude-sonnet-4-20250514",
  "enabledPlugins": {
    "rust-analyzer-lsp@claude-plugins-official": true
  },
  "skipDangerousModePermissionPrompt": true
}
```

### Local Permission Settings (`~/.claude/settings.local.json`)
```json
{
  "permissions": {
    "allow": [
      "Bash(kubectl logs:*)",
      "Bash(kubectl apply:*)",
      "Bash(kubectl wait:*)",
      "Bash(kubectl exec:*)",
      "Bash(kubectl port-forward *)",
      "Bash(curl -s localhost:9101/metrics)",
      "Bash(pkill *)",
      "Bash(kubectl get *)",
      "Bash(jq -r)",
      "Bash(aptos-node -f fullnode-local.yaml)",
      "Bash(aws eks list-clusters *)"
    ],
    "deny": [],
    "ask": []
  }
}
```

## Testing Each Permission Mode

### 1. `default` mode
- **Behavior**: Permissive within working directory, blocks access outside
- **File Access**: Limited to project directory and subdirectories
- **Tool Usage**: All tools available with automatic approval for safe operations

### 2. `dontAsk` mode  
- **Behavior**: Similar to default, executes commands without permission prompts
- **File Access**: Limited to working directory
- **Tool Usage**: All tools available, no user prompts for allowed operations

### 3. `bypassPermissions` mode
- **Behavior**: Allows access throughout entire filesystem  
- **File Access**: Can read files anywhere user has system permissions
- **Tool Usage**: All tools available with minimal restrictions
- **Security**: Respects OS file permissions but ignores Claude's directory restrictions

### 4. `plan` mode
- **Behavior**: Creates detailed implementation plans instead of immediate execution
- **Example**: Asked to "list directory" → created comprehensive plan for directory listing functionality
- **Use Case**: Planning complex implementations before execution

### 5. `--bare` mode
- **Authentication**: Requires API key instead of OAuth/keychain
- **Features Disabled**: Hooks, LSP, plugin sync, auto-memory, background prefetches
- **Security**: Minimal feature set, ANTHROPIC_API_KEY only for auth

### 6. `acceptEdits` mode
- **Behavior**: Automatically approves file edit operations
- **File Access**: Can edit files without user confirmation
- **Tool Usage**: Edit and Write tools work automatically
- **Use Case**: Automated file modifications without prompts

### 7. `auto` mode
- **Behavior**: (Similar to default) Automatic approval for common operations
- **Intelligence**: Decides based on operation safety and context
- **File Access**: Project directory focused
- **Tool Usage**: Smart approval based on operation type

## Tool Access Control Testing

### `--allowed-tools` Testing
- **Command**: `--allowed-tools "Bash,Read"`
- **Result**: ✅ Successfully restricted to only Bash and Read tools
- **Behavior**: Other tools (Edit, Write, etc.) not available
- **Use Case**: Limiting Claude to read-only operations

### `--disallowed-tools` Testing  
- **Command**: `--disallowed-tools "Edit,Write"`
- **Result**: ✅ Successfully blocked Edit and Write tools
- **Behavior**: Claude attempted alternative approaches but was restricted
- **Error Messages**: Clear indication of blocked operations
- **Use Case**: Preventing file modifications while allowing other operations

## Concrete Configuration Examples

### 1. Read-Only Mode
```bash
claude --permission-mode default --disallowed-tools "Edit,Write" "Analyze this codebase"
```
- Allows code exploration and analysis
- Prevents any file modifications
- Ideal for code review or investigation

### 2. Automated Development Mode  
```bash
claude --permission-mode acceptEdits --allowed-tools "Bash,Edit,Write,Read" "Implement feature X"
```
- Auto-approves file edits  
- Restricted to essential development tools
- Good for trusted automation

### 3. Filesystem-Wide Access
```bash
claude --permission-mode bypassPermissions "System-wide file analysis"
```
- Access to entire filesystem (within OS permissions)
- No Claude-imposed directory restrictions
- Use with caution - high privilege mode

### 4. Planning Mode
```bash
claude --permission-mode plan "Design a new authentication system" 
```
- Creates detailed implementation plans
- No immediate code execution
- Good for architecture and design work

### 5. Minimal Security Mode (Sandboxed)
```bash
claude --bare --dangerously-skip-permissions --system-prompt "Custom prompt" 
```
- Requires API key authentication
- Bypasses all permission checks
- Minimal feature set
- ONLY for isolated/sandboxed environments

## Permission Configuration Files

### Setting Up Project-Specific Permissions
Create `.claude/settings.json` in your project root:
```json
{
  "permissions": {
    "allow": [
      "Bash(npm *)",
      "Bash(git *)",
      "Edit(*.js)",
      "Edit(*.ts)",
      "Write(src/*)"
    ],
    "deny": [
      "Bash(rm -rf *)",
      "Edit(package.json)"
    ],
    "ask": [
      "Bash(curl *)",
      "Write(config/*)"
    ]
  }
}
```

### Setting Up User-Wide Permissions  
Edit `~/.claude/settings.local.json`:
```json
{
  "permissions": {
    "allow": [
      "Bash(kubectl *)",
      "Bash(docker *)", 
      "Read(/etc/*)",
      "Bash(systemctl status *)"
    ],
    "deny": [
      "Bash(sudo *)",
      "Edit(/etc/*)"
    ]
  },
  "skipDangerousModePermissionPrompt": false
}
```

## Key Findings Summary

1. **Permission Modes**: 7 different modes with distinct behaviors
2. **Configuration Hierarchy**: Global → Local → Project settings
3. **Tool Control**: Granular allow/deny for specific tools and patterns  
4. **Pattern Matching**: Supports wildcards and command patterns
5. **Safety Features**: Built-in dangerous operation detection
6. **Authentication Options**: OAuth vs API key for different security models

## Recommendations

- **Development**: Use `default` or `acceptEdits` for trusted projects
- **Code Review**: Use `default` with `--disallowed-tools "Edit,Write"`  
- **System Administration**: Use `bypassPermissions` carefully with specific tool allowlists
- **Planning**: Use `plan` mode for complex architecture decisions
- **Automation/CI**: Use `--bare` with API key authentication
- **Production**: Always configure explicit allow/deny lists in settings files
