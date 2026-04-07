use anyhow::Result;
use valr_common::{Credentials, ValrClient};

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt().init();
    
    let creds = Credentials::load()?;
    let client = ValrClient::new(creds);
    
    println!("🗑️  Cancelling all SOLUSDTPERP orders...");
    match client.delete("/v1/orders/SOLUSDTPERP", &serde_json::json!({})).await {
        Ok(_) => println!("✅ Orders cancelled"),
        Err(e) => println!("❌ Error: {}", e),
    }
    
    let positions = client.get("/v1/positions/open").await?;
    println!("\n📊 Open positions: {}", positions);
    
    Ok(())
}
