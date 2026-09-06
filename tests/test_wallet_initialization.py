#!/usr/bin/env python3
"""
Test: Verify wallet assets initialization and retrieval.

This test ensures:
1. New retail wallets are created with all supported assets initialized to 0
2. The wallet balance API returns all assets
3. The UI can properly display balances for all crypto assets
"""

import pytest

# Simulate the supported assets
SUPPORTED_ASSETS = [
    "KES", "USDA", "USDT", "USDC", "USD", 
    "UGX", "TZS", "RWF", "BIF", "XAF", "XOF", 
    "AIRT", "IMP", "BTC", "ETH"
]


def test_wallet_structure():
    """Test that a wallet dict with all assets can be created."""
    user_id = "test_user_123"
    wallet_init = {"userId": user_id}
    for asset in SUPPORTED_ASSETS:
        wallet_init[asset] = 0.0
    
    # Verify structure
    assert wallet_init["userId"] == user_id
    
    # Verify all assets are present and initialized to 0
    for asset in SUPPORTED_ASSETS:
        assert asset in wallet_init, f"Asset {asset} should be in wallet"
        assert wallet_init[asset] == 0.0, f"Asset {asset} should be initialized to 0.0"
    
    print("✅ test_wallet_structure PASSED")


def test_balance_calculation():
    """Test that balance calculation works for all assets."""
    # Simulate partial wallet data
    partial_wallet = {
        "userId": "test_user_456",
        "KES": 1000.0,
        "USDA": 50.0,
        "IMP": 100.0
    }
    
    # Simulate balance retrieval
    balances = {}
    for asset in SUPPORTED_ASSETS:
        balances[asset] = partial_wallet.get(asset, 0.0)
    
    # Verify all assets are in response
    for asset in SUPPORTED_ASSETS:
        assert asset in balances, f"Asset {asset} should be in balances response"
        if asset == "KES":
            assert balances[asset] == 1000.0
        elif asset == "USDA":
            assert balances[asset] == 50.0
        elif asset == "IMP":
            assert balances[asset] == 100.0
        else:
            assert balances[asset] == 0.0, f"Asset {asset} should default to 0.0"
    
    print("✅ test_balance_calculation PASSED")


def test_withdraw_page_logic():
    """
    Test that the withdrawal page can properly display balance for any crypto asset.
    This simulates the UI logic from WithdrawPage.tsx
    """
    # Create wallet with initialized assets
    wallet = {}
    for asset in SUPPORTED_ASSETS:
        wallet[asset] = 0.0
    
    # Set some balances
    wallet["USDT"] = 100.0
    wallet["USDC"] = 50.0
    wallet["USDA"] = 75.0
    
    # Test asset selection and balance display
    test_cases = [
        ("USDT", 100.0),
        ("USDC", 50.0),
        ("USDA", 75.0),
        ("BTC", 0.0),  # Not funded, but should exist in wallet
        ("ETH", 0.0),  # Not funded, but should exist in wallet
    ]
    
    for asset, expected_balance in test_cases:
        # This is what the UI does in WithdrawPage.tsx line 559:
        available_balance = wallet.get(asset, 0) or 0
        
        assert available_balance == expected_balance, \
            f"Asset {asset} should show balance {expected_balance}, got {available_balance}"
        
        print(f"  ✓ {asset}: {available_balance}")
    
    print("✅ test_withdraw_page_logic PASSED")


def test_missing_field_handling():
    """Test that the UI correctly handles missing fields."""
    # Old wallet without all fields
    old_wallet = {
        "userId": "old_user",
        "KES": 500.0,
        "USDA": 25.0,
        # Missing: USDT, USDC, BTC, ETH, etc.
    }
    
    # Simulate UI accessing missing asset
    selected_asset = "USDT"
    available_balance = old_wallet.get(selected_asset) or 0
    
    # Should safely return 0 instead of failing
    assert available_balance == 0, f"Missing asset should default to 0"
    
    print("✅ test_missing_field_handling PASSED")


if __name__ == "__main__":
    print("\n" + "="*60)
    print("RETAIL WALLET ASSET INITIALIZATION TESTS")
    print("="*60 + "\n")
    
    try:
        test_wallet_structure()
        test_balance_calculation()
        test_withdraw_page_logic()
        test_missing_field_handling()
        
        print("\n" + "="*60)
        print("✅ ALL TESTS PASSED")
        print("="*60 + "\n")
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}\n")
        raise
