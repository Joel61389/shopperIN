import base64
import json
import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from django.urls import reverse

from .models import CoinTransaction, MpesaTransaction, Order, Product, Review, User


class StorefrontTests(TestCase):
	def setUp(self):
		self.vendor = User.objects.create_user(username="seller", password="password123", phone_number="254712345678", role="VENDOR")
		self.customer = User.objects.create_user(username="buyer", password="password123", phone_number="254722222222")
		self.product = Product.objects.create(vendor=self.vendor, name="Market basket", description="Fresh produce", price="250.00", stock=4)

	def test_store_search_and_type_filter(self):
		Product.objects.create(vendor=self.vendor, category="CONCERTS", name="Music night", price="800.00", stock=10)
		response = self.client.get(reverse("home"), {"q": "music", "type": "tickets"})
		self.assertContains(response, "Music night")
		self.assertNotContains(response, "Market basket")

	def test_store_filters_by_category_and_listing_type(self):
		concert = Product.objects.create(vendor=self.vendor, category="CONCERTS", name="Live show", price="400.00", stock=5)
		product_response = self.client.get(reverse("home"), {"type": "products"})
		ticket_response = self.client.get(reverse("home"), {"type": "tickets", "category": "CONCERTS"})
		self.assertContains(product_response, "Market basket")
		self.assertNotContains(product_response, "Live show")
		self.assertContains(ticket_response, "Live show")
		self.assertTrue(concert.is_ticket)

	def test_cart_add_update_and_clear_sync_session_totals(self):
		self.product.price = "250.25"
		self.product.save(update_fields=["price"])
		add = self.client.post(reverse("cart_add"), data=json.dumps({"product_id": self.product.id, "qty": 2}), content_type="application/json")
		self.assertEqual(add.status_code, 200)
		self.assertEqual(add.json()["count"], 2)
		self.assertEqual(add.json()["total"], "501.00")
		update = self.client.post(reverse("cart_update"), data=json.dumps({"product_id": self.product.id, "qty": 3}), content_type="application/json")
		self.assertEqual(update.json()["items"][0]["qty"], 3)
		overstock = self.client.post(reverse("cart_update"), data=json.dumps({"product_id": self.product.id, "qty": 5}), content_type="application/json")
		self.assertEqual(overstock.status_code, 400)
		clear = self.client.post(reverse("cart_clear"), data="{}", content_type="application/json")
		self.assertEqual(clear.json()["count"], 0)

	@patch("myapp.views.initiate_stk_push", return_value={"ResponseCode": "0", "CheckoutRequestID": "cart-checkout-1"})
	def test_cart_checkout_creates_batch_and_single_stk_request(self, stk_push):
		second_product = Product.objects.create(vendor=self.vendor, name="Tea", price="125.00", stock=8)
		self.client.post(reverse("cart_add"), data=json.dumps({"product_id": self.product.id, "qty": 2}), content_type="application/json")
		self.client.post(reverse("cart_add"), data=json.dumps({"product_id": second_product.id, "qty": 1}), content_type="application/json")
		self.client.force_login(self.customer)
		response = self.client.post(reverse("cart_checkout"), {"phone_number": self.customer.phone_number})
		orders = list(Order.objects.filter(customer=self.customer).order_by("created_at"))
		self.assertEqual(len(orders), 2)
		self.assertEqual(orders[0].batch_reference, orders[1].batch_reference)
		self.assertRedirects(response, reverse("payment_waiting", args=[orders[0].id]))
		self.assertEqual(stk_push.call_count, 1)
		self.assertEqual(stk_push.call_args.kwargs["amount"], 625)
		self.assertEqual(self.client.session.get("cart"), {})
		self.assertTrue(MpesaTransaction.objects.filter(batch_reference=orders[0].batch_reference).exists())

	@patch("myapp.views.initiate_stk_push", return_value={"ResponseCode": "0", "CheckoutRequestID": "coin-cart-checkout"})
	def test_cart_coin_redemption_is_capped_at_twenty_percent(self, stk_push):
		self.product.price = "1000.00"
		self.product.save(update_fields=["price"])
		self.customer.coin_balance = 500
		self.customer.save(update_fields=["coin_balance"])
		self.client.post(reverse("cart_add"), data=json.dumps({"product_id": self.product.id, "qty": 1}), content_type="application/json")
		self.client.force_login(self.customer)
		response = self.client.post(reverse("cart_checkout"), {"phone_number": self.customer.phone_number, "redeem_coins": "1"})
		self.assertRedirects(response, reverse("payment_waiting", args=[Order.objects.get().id]))
		self.assertEqual(stk_push.call_args.kwargs["amount"], 800)
		order = Order.objects.get()
		self.assertEqual(order.coins_redeemed, 200)
		self.assertEqual(order.total_amount, 1000)
		self.customer.refresh_from_db()
		self.assertEqual(self.customer.coin_balance, 300)
		self.assertTrue(CoinTransaction.objects.filter(user=self.customer, amount=-200).exists())

	@patch("myapp.views.initiate_stk_push", return_value={"ResponseCode": "1", "errorMessage": "Rejected"})
	def test_cart_coin_redemption_is_refunded_when_payment_is_rejected(self, stk_push):
		self.product.price = "1000.00"
		self.product.save(update_fields=["price"])
		self.customer.coin_balance = 300
		self.customer.save(update_fields=["coin_balance"])
		self.client.post(reverse("cart_add"), data=json.dumps({"product_id": self.product.id, "qty": 1}), content_type="application/json")
		self.client.force_login(self.customer)
		self.client.post(reverse("cart_checkout"), {"phone_number": self.customer.phone_number, "redeem_coins": "1"})
		self.customer.refresh_from_db()
		self.assertEqual(self.customer.coin_balance, 300)
		self.assertEqual(sum(CoinTransaction.objects.filter(user=self.customer).values_list("amount", flat=True)), 0)

	def test_only_paid_buyer_can_create_or_update_review(self):
		self.client.force_login(self.customer)
		url = reverse("submit_review", args=[self.product.id])
		denied = self.client.post(url, {"rating": "5", "comment": "Lovely"})
		self.assertEqual(denied.status_code, 403)
		order = Order.objects.create(customer=self.customer, product=self.product, quantity=1, total_amount="250.00", phone_number=self.customer.phone_number, status="PAID")
		first = self.client.post(url, {"rating": "5", "comment": "Lovely"})
		second = self.client.post(url, {"rating": "4", "comment": "Still great"})
		self.assertEqual(first.status_code, 302)
		self.assertEqual(second.status_code, 302)
		self.assertEqual(Review.objects.filter(product=self.product, user=self.customer).count(), 1)
		self.customer.refresh_from_db()
		self.vendor.refresh_from_db()
		self.assertEqual(self.customer.coin_balance, 15)
		self.assertEqual(self.vendor.coin_balance, 10)
		self.product.refresh_from_db()
		self.assertEqual(self.product.average_rating, 4)
		self.assertEqual(self.product.review_count, 1)
		orders_response = self.client.get(reverse("order_history"))
		self.assertContains(orders_response, "Update review")
		self.assertContains(orders_response, "Still great")

	def test_paid_ticket_order_history_renders_qr_pass(self):
		ticket = Product.objects.create(vendor=self.vendor, category="CONCERTS", name="Friday show", price="500.00", stock=10)
		order = Order.objects.create(customer=self.customer, product=ticket, quantity=1, total_amount="500.00", phone_number=self.customer.phone_number, status="PAID")
		MpesaTransaction.objects.create(order=order, checkout_request_id="ticket-qr", mpesa_receipt_number="QR12345")
		self.client.force_login(self.customer)
		response = self.client.get(reverse("order_history"))
		self.assertContains(response, "Event entry pass")
		self.assertContains(response, "data:image/svg+xml;base64,")
		self.assertContains(response, "QR12345")

	def test_featured_listing_costs_100_coins_and_expires_after_48_hours(self):
		self.vendor.coin_balance = 150
		self.vendor.save(update_fields=["coin_balance"])
		self.client.force_login(self.vendor)
		response = self.client.post(reverse("feature_product", args=[self.product.id]))
		self.assertRedirects(response, reverse("vendor_dashboard"))
		self.product.refresh_from_db()
		self.vendor.refresh_from_db()
		self.assertEqual(self.vendor.coin_balance, 50)
		self.assertAlmostEqual((self.product.featured_until - timezone.now()).total_seconds(), 48 * 60 * 60, delta=5)
		self.assertTrue(CoinTransaction.objects.filter(user=self.vendor, amount=-100).exists())

	def test_verification_eligibility_checks_orders_reviews_and_active_listings(self):
		self.assertFalse(self.vendor.check_verification_eligibility())
		Product.objects.create(vendor=self.vendor, name="Second listing", price="25.00", stock=2)
		Product.objects.create(vendor=self.vendor, name="Third listing", price="35.00", stock=2)
		for _ in range(5):
			Order.objects.create(customer=self.customer, product=self.product, quantity=1, total_amount="250.00", phone_number=self.customer.phone_number, status="PAID")
		for index, rating in enumerate((5, 4, 4), start=1):
			reviewer = User.objects.create_user(username=f"reviewer{index}", password="password123", phone_number=f"25473333333{index}")
			Order.objects.create(customer=reviewer, product=self.product, quantity=1, total_amount="250.00", phone_number=reviewer.phone_number, status="PAID")
			Review.objects.create(product=self.product, user=reviewer, rating=rating)
		self.assertTrue(self.vendor.check_verification_eligibility())

	def test_vendor_form_exposes_all_grouped_categories(self):
		self.client.force_login(self.vendor)
		response = self.client.get(reverse("vendor_dashboard"))
		self.assertContains(response, "Marketplace &amp; Groceries")
		self.assertContains(response, "Events &amp; Tickets")
		self.assertContains(response, "Concerts &amp; Nightlife")

	@override_settings(GEMINI_API_KEY="")
	def test_description_generator_returns_fallback_without_api_key(self):
		self.client.force_login(self.vendor)
		response = self.client.post(
			reverse("generate_description"),
			data=json.dumps({"name": "Market basket", "price": "250", "category": "Product"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		self.assertIn("Market basket", response.json()["description"])
		self.assertIn("Kenya", response.json()["description"])
		self.assertTrue(response.json()["fallback"])

	@override_settings(GEMINI_API_KEY="test-api-key")
	@patch("myapp.views.requests.post")
	def test_description_generator_calls_gemini(self, post):
		post.return_value.raise_for_status.return_value = None
		post.return_value.json.return_value = {
			"candidates": [{"content": {"parts": [
				{"text": "Internal planning text", "thought": True},
				{"text": "A fresh market basket for your home. Shop locally in Kenya."},
			]}}]
		}
		self.client.force_login(self.vendor)
		response = self.client.post(
			reverse("generate_description"),
			data=json.dumps({"name": "Market basket", "price": "250", "category": "Product"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["description"], "A fresh market basket for your home. Shop locally in Kenya.")
		self.assertFalse(response.json()["fallback"])
		self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-api-key")

	@override_settings(GEMINI_API_KEY="test-api-key")
	@patch("myapp.views.requests.post")
	def test_description_generator_sends_uploaded_photo_to_gemini(self, post):
		post.return_value.raise_for_status.return_value = None
		post.return_value.json.return_value = {
			"candidates": [{"content": {"parts": [{"text": json.dumps({
				"mismatch": False,
				"description": "A bright note for your kitchen. Fresh produce for local homes.",
			})}]}}]
		}
		self.client.force_login(self.vendor)
		image_bytes = b"sample-image-bytes"
		response = self.client.post(reverse("generate_description"), {
			"name": "Fresh carrots",
			"category": "GROCERIES",
			"price": "120",
			"image": SimpleUploadedFile("carrots.png", image_bytes, content_type="image/png"),
		})
		parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json()["description"], "A bright note for your kitchen. Fresh produce for local homes.")
		self.assertEqual(parts[1]["inline_data"]["mime_type"], "image/png")
		self.assertEqual(base64.b64decode(parts[1]["inline_data"]["data"]), image_bytes)
		self.assertIn("Visually inspect the attached image first", parts[0]["text"])

	@override_settings(GEMINI_API_KEY="test-api-key")
	@patch("myapp.views.requests.post")
	def test_description_generator_returns_photo_mismatch_without_description(self, post):
		post.return_value.raise_for_status.return_value = None
		message = "Heads up: the uploaded photo looks like carrots, which doesn't match 'Samsung S26'. Please double-check your photo or product title."
		post.return_value.json.return_value = {
			"candidates": [{"content": {"parts": [{"text": json.dumps({"mismatch": True, "message": message})}]}}]
		}
		self.client.force_login(self.vendor)
		response = self.client.post(
			reverse("generate_description"),
			data=json.dumps({"name": "Samsung S26", "category": "TECH", "price": "999"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json(), {"mismatch": True, "message": message})

	@override_settings(GEMINI_API_KEY="invalid-key")
	@patch("myapp.views.requests.post", side_effect=requests.RequestException("API unavailable"))
	def test_description_generator_falls_back_when_gemini_fails(self, post):
		self.client.force_login(self.vendor)
		response = self.client.post(
			reverse("generate_description"),
			data=json.dumps({"name": "Market basket", "price": "250", "category": "Product"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.json()["fallback"])
		self.assertIn("Market basket", response.json()["description"])

	def test_vendor_dashboard_renders_generator_button(self):
		self.client.force_login(self.vendor)
		response = self.client.get(reverse("vendor_dashboard"))
		self.assertContains(response, "Refresh AI Description")
		self.assertContains(response, "My Listings")
		self.assertContains(response, reverse("generate_description"))

	def test_vendor_can_edit_own_listing(self):
		self.client.force_login(self.vendor)
		edit_page = self.client.get(reverse("vendor_dashboard"), {"edit": self.product.id})
		self.assertContains(edit_page, "Save changes")
		response = self.client.post(reverse("vendor_dashboard"), {
			"product_id": self.product.id,
			"category": "GROCERIES",
			"name": "Edited market basket",
			"description": "A refreshed listing description.",
			"price": "275.00",
			"stock": "6",
		})
		self.assertRedirects(response, reverse("vendor_dashboard"))
		self.product.refresh_from_db()
		self.assertEqual(self.product.name, "Edited market basket")
		self.assertEqual(self.product.price, 275)

	def test_description_generator_requires_vendor_account(self):
		self.client.force_login(self.customer)
		response = self.client.post(
			reverse("generate_description"),
			data=json.dumps({"name": "Market basket", "price": "250", "category": "Product"}),
			content_type="application/json",
		)
		self.assertEqual(response.status_code, 403)

	def test_vendor_can_update_own_stock_but_not_other_vendor_listing(self):
		other_vendor = User.objects.create_user(username="other", password="password123", phone_number="254733333333", role="VENDOR")
		other_product = Product.objects.create(vendor=other_vendor, name="Other item", price="15.00", stock=7)
		self.client.force_login(self.vendor)

		response = self.client.post(reverse("vendor_dashboard"), {"action": "update_stock", "product_id": self.product.id, "stock": 9})
		self.assertRedirects(response, reverse("vendor_dashboard"))
		self.product.refresh_from_db()
		self.assertEqual(self.product.stock, 9)

		denied = self.client.post(reverse("vendor_dashboard"), {"action": "update_stock", "product_id": other_product.id, "stock": 0})
		self.assertEqual(denied.status_code, 404)
		other_product.refresh_from_db()
		self.assertEqual(other_product.stock, 7)

	@patch("myapp.views.initiate_stk_push", return_value={"ResponseCode": "0", "CheckoutRequestID": "checkout-123"})
	def test_checkout_creates_pending_order_and_waiting_redirect(self, stk_push):
		self.product.price = "250.25"
		self.product.save(update_fields=["price"])
		self.client.force_login(self.customer)
		response = self.client.post(reverse("checkout", args=[self.product.id]), {"quantity": "2", "phone_number": "254712345678"})
		order = Order.objects.get(customer=self.customer)
		self.assertRedirects(response, reverse("payment_waiting", args=[order.id]))
		self.assertEqual(order.total_amount, 501)
		self.assertTrue(MpesaTransaction.objects.filter(order=order, checkout_request_id="checkout-123").exists())
		self.assertEqual(stk_push.call_args.kwargs["amount"], 501)

	@patch("myapp.views.initiate_stk_push")
	def test_checkout_rejects_invalid_phone_before_payment_request(self, stk_push):
		self.client.force_login(self.customer)
		response = self.client.post(reverse("checkout", args=[self.product.id]), {"quantity": "1", "phone_number": "123"})
		self.assertRedirects(response, reverse("home"))
		self.assertFalse(Order.objects.exists())
		stk_push.assert_not_called()

	def test_callback_marks_paid_and_receipt_is_visible_to_customer(self):
		order = Order.objects.create(customer=self.customer, product=self.product, quantity=1, total_amount="250.00", phone_number=self.customer.phone_number)
		MpesaTransaction.objects.create(order=order, checkout_request_id="checkout-456")
		callback = {
			"Body": {"stkCallback": {
				"CheckoutRequestID": "checkout-456",
				"ResultCode": 0,
				"ResultDesc": "Success",
				"CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "ABC123XYZ"}]},
			}},
		}

		response = self.client.post(reverse("mpesa_callback"), data=json.dumps(callback), content_type="application/json")
		self.assertEqual(response.status_code, 200)
		order.refresh_from_db()
		self.assertEqual(order.status, "PAID")
		self.client.force_login(self.customer)
		receipt_response = self.client.get(reverse("receipt", args=[order.id]))
		self.assertContains(receipt_response, "ABC123XYZ")

	def test_batch_callback_marks_all_orders_paid_and_deducts_stock(self):
		second_product = Product.objects.create(vendor=self.vendor, name="Tea", price="125.00", stock=8)
		batch = "d8e8d6a1-8e18-475a-b1c6-b821cd006421"
		first = Order.objects.create(customer=self.customer, product=self.product, quantity=2, total_amount="500.00", phone_number=self.customer.phone_number, batch_reference=batch)
		second = Order.objects.create(customer=self.customer, product=second_product, quantity=1, total_amount="125.00", phone_number=self.customer.phone_number, batch_reference=batch)
		MpesaTransaction.objects.create(order=first, batch_reference=batch, checkout_request_id="batch-callback")
		callback = {"Body": {"stkCallback": {"CheckoutRequestID": "batch-callback", "ResultCode": 0, "ResultDesc": "Success", "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "BATCH123"}]}}}}
		response = self.client.post(reverse("mpesa_callback"), data=json.dumps(callback), content_type="application/json")
		self.assertEqual(response.status_code, 200)
		first.refresh_from_db()
		second.refresh_from_db()
		self.product.refresh_from_db()
		second_product.refresh_from_db()
		self.assertEqual((first.status, second.status), ("PAID", "PAID"))
		self.assertEqual((self.product.stock, second_product.stock), (2, 7))
		self.client.force_login(self.customer)
		history = self.client.get(reverse("order_history"))
		self.assertEqual(history.content.count(b"BATCH123"), 2)

	def test_paid_callback_awards_purchase_coins_only_once(self):
		order = Order.objects.create(customer=self.customer, product=self.product, quantity=1, total_amount="250.00", phone_number=self.customer.phone_number)
		MpesaTransaction.objects.create(order=order, checkout_request_id="coin-callback")
		callback = {"Body": {"stkCallback": {"CheckoutRequestID": "coin-callback", "ResultCode": 0, "ResultDesc": "Success", "CallbackMetadata": {"Item": []}}}}
		self.client.post(reverse("mpesa_callback"), data=json.dumps(callback), content_type="application/json")
		self.client.post(reverse("mpesa_callback"), data=json.dumps(callback), content_type="application/json")
		self.customer.refresh_from_db()
		self.assertEqual(self.customer.coin_balance, 2)
		self.assertEqual(CoinTransaction.objects.filter(user=self.customer, reason="1 DukaCoin per KES 100 paid").count(), 1)
