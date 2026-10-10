!> A deliberately naive matrix multiply (something for the compiler to chew on).
module demo_matrix
    use demo_kinds, only: wp
    implicit none
    private
    public :: matmul_naive, identity

contains

    pure function identity(n) result(a)
        integer, intent(in) :: n
        real(wp) :: a(n, n)
        integer :: i
        a = 0.0_wp
        do i = 1, n
            a(i, i) = 1.0_wp
        end do
    end function identity

    pure function matmul_naive(a, b) result(c)
        real(wp), intent(in) :: a(:, :), b(:, :)
        real(wp) :: c(size(a, 1), size(b, 2))
        integer :: i, j, k
        c = 0.0_wp
        do j = 1, size(b, 2)
            do k = 1, size(a, 2)
                do i = 1, size(a, 1)
                    c(i, j) = c(i, j) + a(i, k) * b(k, j)
                end do
            end do
        end do
    end function matmul_naive

end module demo_matrix
