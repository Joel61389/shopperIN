
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import CoinTransaction, MpesaTransaction, Order, Product, User
# register your models here.

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("ShopperIN Profile", {"fields": ("phone_number", "role", "is_verified", "id_document", "coin_balance")}),
    )
    list_display = ("username", "email", "phone_number", "role", "is_verified", "coin_balance", "is_staff")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "vendor", "price", "stock", "created_at")
    search_fields = ("name", "vendor__username")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "customer", "product", "total_amount", "status", "created_at")
    list_filter = ("status",)


@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(admin.ModelAdmin):
    list_display = ("checkout_request_id", "mpesa_receipt_number", "result_code", "updated_at")
    readonly_fields = ("raw_callback",)


@admin.register(CoinTransaction)
class CoinTransactionAdmin(admin.ModelAdmin):
    list_display = ("user", "amount", "reason", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__username", "reason")
    readonly_fields = ("user", "amount", "reason", "created_at")