from django import forms

from .models import Chapter, UserProfile

# Implementation detail, NOT a client requirement: API.md §7 requires "a sane size
# ceiling" but no source document gives a number, so this is the build's choice.
MAX_CHAPTER_PDF_BYTES = 50 * 1024 * 1024


class ProfileForm(forms.ModelForm):
    """REQ-DASH-04: the phone number is the only editable profile field.

    Email is displayed from the stock User and password changes stay with
    allauth, so neither is a form field here.
    """

    class Meta:
        model = UserProfile
        fields = ["phone_number"]
        widgets = {
            "phone_number": forms.TextInput(
                attrs={"class": "form-control", "autocomplete": "tel"}
            )
        }


class ChapterAdminForm(forms.ModelForm):
    """Chapter admin form with a non-model PDF upload field (API.md §7).

    `pdf_file` is a plain form field, not a model field. The raw
    `pdf_object_key` is deliberately not editable here: `ChapterAdmin.save_model`
    sets it from the deterministic private key after a successful R2 upload.
    """

    pdf_file = forms.FileField(
        required=False,
        label="PDF file",
        help_text=(
            "Optional. PDF only, up to 50 MB. Uploading a file replaces the "
            "chapter's current PDF."
        ),
    )

    class Meta:
        model = Chapter
        fields = ["book", "title", "order"]

    def clean_pdf_file(self):
        upload = self.cleaned_data.get("pdf_file")
        if not upload:
            return upload
        if not upload.name.lower().endswith(".pdf"):
            raise forms.ValidationError("The file must have a .pdf extension.")
        if (upload.content_type or "").lower() != "application/pdf":
            raise forms.ValidationError("The file must be a PDF (application/pdf).")
        if upload.size > MAX_CHAPTER_PDF_BYTES:
            raise forms.ValidationError(
                f"The file is too large; the limit is {MAX_CHAPTER_PDF_BYTES // (1024 * 1024)} MB."
            )
        # The declared name and type come from the browser; also require the PDF header.
        header = upload.read(1024)
        upload.seek(0)
        if b"%PDF-" not in header:
            raise forms.ValidationError("The file does not look like a valid PDF.")
        return upload
