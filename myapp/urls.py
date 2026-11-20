from django.contrib.auth import views as auth_views
from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register_view, name="register"),
    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("vendor/", views.vendor_dashboard, name="vendor_dashboard"),
    path("vendor/listing/<int:product_id>/feature/", views.feature_product, name="feature_product"),
    path("vendor/generate-description/", views.generate_description, name="generate_description"),
    path("cart/add/", views.cart_add, name="cart_add"),
    path("cart/update/", views.cart_update, name="cart_update"),
    path("cart/clear/", views.cart_clear, name="cart_clear"),
    path("cart/checkout/", views.cart_checkout, name="cart_checkout"),
    path("review/<int:product_id>/", views.submit_review, name="submit_review"),
    path("orders/", views.order_history, name="order_history"),
    path("checkout/<int:product_id>/", views.checkout_stk_push, name="checkout"),
    path("order/<uuid:order_id>/waiting/", views.payment_waiting, name="payment_waiting"),
    path("order/<uuid:order_id>/receipt/", views.receipt, name="receipt"),
    path("order/<uuid:order_id>/status/", views.check_order_status, name="check_order_status"),
    path("mpesa/callback/", views.mpesa_callback, name="mpesa_callback"),
]