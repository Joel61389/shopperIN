
import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Avg, Count
# create your models here.

class User(AbstractUser):
    ROLE_CHOICES = (
        ("CUSTOMER", "Customer"),
        ("VENDOR", "Vendor"),
    )
    phone_number = models.CharField(max_length=15, unique=True, help_text="Format: 2547XXXXXXXX")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default="CUSTOMER")
    is_verified = models.BooleanField(default=False)
    id_document = models.FileField(upload_to="vendor_ids/", blank=True, null=True)
    coin_balance = models.PositiveIntegerField(default=0)

    def check_verification_eligibility(self):
        if self.is_verified:
            return True
        paid_orders_count = Order.objects.filter(product__vendor=self, status="PAID").count()
        review_stats = Review.objects.filter(product__vendor=self).aggregate(
            average=Avg("rating"), count=Count("id")
        )
        active_products_count = self.products.filter(is_active=True).count()
        return (
            paid_orders_count >= 5
            and review_stats["count"] >= 3
            and review_stats["average"] >= 4.2
            and active_products_count >= 3
        )

    def __str__(self):
        return f"{self.username} ({self.role})"


class Product(models.Model):
    TICKET_CATEGORIES = {"CONCERTS", "WORKSHOPS", "SOCIAL", "SPORTS"}
    CATEGORY_CHOICES = [
        ("Marketplace & Groceries", [
            ("GROCERIES", "Fresh Groceries & Produce"),
            ("PANTRY", "Pantry, Bakery & Drinks"),
            ("TECH", "Tech & Gadgets"),
            ("FASHION", "Fashion, Shoes & Beauty"),
            ("HOME", "Home, Kitchen & Furniture"),
            ("HEALTH", "Health, Baby & Personal Care"),
            ("BOOKS", "Books, Stationery & Gifts"),
            ("OTHER", "Other & General"),
        ]),
        ("Events & Tickets", [
            ("CONCERTS", "Concerts & Nightlife"),
            ("WORKSHOPS", "Workshops & Conferences"),
            ("SOCIAL", "Social, Brunch & Comedy"),
            ("SPORTS", "Campus, Sports & Outdoor"),
        ]),
    ]

    vendor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="products")
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="GROCERIES")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveIntegerField(default=10)
    is_active = models.BooleanField(default=True)
    image = models.ImageField(upload_to="products/", blank=True, null=True)
    featured_until = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_ticket(self):
        return self.category in self.TICKET_CATEGORIES

    @property
    def average_rating(self):
        return self.reviews.aggregate(value=Avg("rating"))["value"] or 0

    @property
    def review_count(self):
        return self.reviews.count()

    def __str__(self):
        return f"{self.name} - KES {self.price}"


class Order(models.Model):
    STATUS_CHOICES = (
        ("PENDING", "Pending Payment"),
        ("PAID", "Paid"),
        ("FAILED", "Failed / Cancelled"),
    )
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch_reference = models.UUIDField(null=True, blank=True, db_index=True)
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField(default=1)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    coins_redeemed = models.PositiveIntegerField(default=0)
    phone_number = models.CharField(max_length=15)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="PENDING")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Order {str(self.id)[:8]} ({self.status})"


class CoinTransaction(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="coin_transactions")
    amount = models.IntegerField()
    reason = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user.username}: {self.amount:+d} DukaCoins"


class MpesaTransaction(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="mpesa_tx")
    batch_reference = models.UUIDField(null=True, blank=True, db_index=True)
    merchant_request_id = models.CharField(max_length=100, blank=True)
    checkout_request_id = models.CharField(max_length=100, unique=True)
    mpesa_receipt_number = models.CharField(max_length=50, blank=True, null=True)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.TextField(blank=True)
    raw_callback = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.checkout_request_id} - {self.mpesa_receipt_number or 'Pending'}"


class Review(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="product_reviews")
    rating = models.PositiveSmallIntegerField()
    comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        unique_together = (("product", "user"),)

    def __str__(self):
        return f"{self.product.name}: {self.rating}/5 by {self.user.username}"