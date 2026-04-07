use std::process::Command;

fn load_secret(name: &str) -> Result<String, Box<dyn std::error::Error>> {
    let output = Command::new("python3")
        .args(["/home/admin/.openclaw/secrets/secrets.py", "get", name])
        .output()?;
    if !output.status.success() {
        return Err(format!("Failed to read {}: {}", name, String::from_utf8_lossy(&output.stderr)).into());
    }
    Ok(String::from_utf8(output.stdout)?.trim().to_string())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("Testing secret loading...");
    let key = load_secret("valr_main_api_key")?;
    let secret = load_secret("valr_main_api_secret")?;
    println!("Key length: {}", key.len());
    println!("Secret length: {}", secret.len());
    println!("Success!");
    Ok(())
}