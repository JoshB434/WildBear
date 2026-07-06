# Cleanup script to cancel pending test orders and close test positions
# Usage: ./cleanup_test_positions.ps1 -symbols "TSLA,AAPL,SPY" or ./cleanup_test_positions.ps1 (clears all)

param(
    [string]$symbols = ""  # Comma-separated list of test symbols, or empty to clean all
)

$apiKey = "PKD7FPV7WNLAFFANMC4FLHXPGD"
$apiSecret = "oyD2KuEC2J9WYoRgRs7xRQuToxJ4vmCjExmZsnf7HGp"
$baseUrl = "https://paper-api.alpaca.markets/v2"

$headers = @{
    "APCA-API-KEY-ID" = $apiKey
    "APCA-API-SECRET-KEY" = $apiSecret
}

Write-Host "╔═══════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║      SMOKE TEST CLEANUP - Cancel Orders & Close Positions   ║" -ForegroundColor Cyan
Write-Host "╚═══════════════════════════════════════════════════════════╝" -ForegroundColor Cyan

# Parse symbols to clean
$testSymbols = @()
if ($symbols) {
    $testSymbols = @($symbols.Split(",") | ForEach-Object { $_.Trim().ToUpper() })
    Write-Host "Target symbols: $($testSymbols -join ', ')" -ForegroundColor Yellow
} else {
    Write-Host "No symbols specified - will clean ALL pending orders and positions" -ForegroundColor Yellow
}

# 1. Cancel all pending orders
Write-Host "`n[1/2] Cancelling pending orders..." -ForegroundColor Cyan
try {
    $orders = Invoke-RestMethod "$baseUrl/orders?status=open" -Headers $headers -ErrorAction Stop
    
    if ($orders.Count -eq 0) {
        Write-Host "  ✓ No pending orders" -ForegroundColor Green
    } else {
        $cancelled = 0
        foreach ($order in $orders) {
            if ($testSymbols.Count -eq 0 -or $order.symbol -in $testSymbols) {
                try {
                    Invoke-RestMethod "$baseUrl/orders/$($order.id)" -Method DELETE -Headers $headers -ErrorAction Stop | Out-Null
                    Write-Host "  ✓ Cancelled: $($order.symbol) $($order.side.ToUpper()) $($order.qty) shares" -ForegroundColor Green
                    $cancelled++
                } catch {
                    Write-Host "  ✗ Failed to cancel $($order.symbol): $_" -ForegroundColor Red
                }
            }
        }
        Write-Host "  Total cancelled: $cancelled" -ForegroundColor Cyan
    }
} catch {
    Write-Host "  ✗ Error fetching orders: $_" -ForegroundColor Red
}

# 2. Close open positions
Write-Host "`n[2/2] Closing open positions..." -ForegroundColor Cyan
try {
    $positions = Invoke-RestMethod "$baseUrl/positions" -Headers $headers -ErrorAction Stop
    
    if ($positions.Count -eq 0) {
        Write-Host "  ✓ No open positions" -ForegroundColor Green
    } else {
        $closed = 0
        foreach ($position in $positions) {
            if ($testSymbols.Count -eq 0 -or $position.symbol -in $testSymbols) {
                try {
                    $qty = [Math]::Abs($position.qty)
                    $side = if ([double]$position.qty -gt 0) { "sell" } else { "buy" }
                    
                    $orderBody = @{
                        symbol = $position.symbol
                        qty = $qty
                        side = $side
                        type = "market"
                        time_in_force = "day"
                    } | ConvertTo-Json
                    
                    Invoke-RestMethod "$baseUrl/orders" -Method POST -Headers $headers -ContentType "application/json" -Body $orderBody -ErrorAction Stop | Out-Null
                    Write-Host "  ✓ Closed: $($position.symbol) $qty shares (was $($position.qty))" -ForegroundColor Green
                    $closed++
                } catch {
                    Write-Host "  ✗ Failed to close $($position.symbol): $_" -ForegroundColor Red
                }
            }
        }
        Write-Host "  Total closed: $closed" -ForegroundColor Cyan
    }
} catch {
    Write-Host "  ✗ Error fetching positions: $_" -ForegroundColor Red
}

Write-Host "`n✓ Cleanup complete!" -ForegroundColor Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
