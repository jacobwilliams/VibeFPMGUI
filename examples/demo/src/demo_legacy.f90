!> Old-style code that compiles cleanly but triggers warnings with the
!> "warnings" feature (-Wall -Wextra): unused variables and dummy arguments,
!> and comparing reals for equality.
module demo_legacy
    use demo_kinds, only: wp
    implicit none
    private
    public :: legacy_scale

contains

    real(wp) function legacy_scale(x, unused_flag)
        real(wp), intent(in) :: x
        integer, intent(in) :: unused_flag
        integer :: never_used
        real(wp) :: tmp
        tmp = x * 2.0_wp
        if (tmp == 0.0_wp) then
            legacy_scale = 0.0_wp
        else
            legacy_scale = tmp
        end if
    end function legacy_scale

end module demo_legacy
